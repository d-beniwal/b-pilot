/* Keep the page current.
 *
 * Poll a tiny `version` endpoint and re-fetch the body only when the report's
 * digest actually changes. Deliberately not server-sent events: an APS-internal
 * reverse proxy running nginx's default `proxy_buffering on` swallows an event
 * stream entirely, and a polling fallback would have been needed anyway. This
 * is ~80 bytes every few seconds with no long-lived connection to keep alive.
 *
 * Everything degrades to "the page you already have". A failed poll is ignored
 * and retried; the age line is the honest signal that something is stale.
 */
(function () {
  "use strict";

  var POLL_MS = 5000;
  var stampEl = document.getElementById("stamp");
  var ageEl = document.getElementById("age");
  var reportEl = document.getElementById("report");
  var current = null;
  var failures = 0;

  function tick() {
    fetch("version", { credentials: "omit", cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error(String(r.status));
        return r.json();
      })
      .then(function (v) {
        failures = 0;
        if (ageEl) ageEl.textContent = v.age || "";
        if (current === null) {
          current = v.sha256;
          return;
        }
        if (v.sha256 && v.sha256 !== current) {
          current = v.sha256;
          return refresh(v);
        }
      })
      .catch(function () {
        failures += 1;
        if (ageEl && failures > 2) ageEl.textContent = "reconnecting…";
      });
  }

  function refresh(v) {
    return fetch("fragment", { credentials: "omit", cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error(String(r.status));
        return r.text();
      })
      .then(function (html) {
        // The fragment is server-rendered from the same escaping-and-allowlist
        // renderer that produced the initial page; no report text reaches the
        // DOM without having gone through it.
        reportEl.innerHTML = html;
        if (stampEl) {
          stampEl.textContent =
            "updated " +
            new Date((v.received_at || 0) * 1000).toLocaleTimeString([], {
              hour: "2-digit",
              minute: "2-digit"
            });
        }
      })
      .catch(function () {
        /* keep what's on screen */
      });
  }

  tick();
  setInterval(tick, POLL_MS);
})();
