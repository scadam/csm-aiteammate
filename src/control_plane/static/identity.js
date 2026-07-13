/*
 * Shared identity client for the CSM Autopilot dashboards.
 *
 * Resolves "who am I" the way a Teams-embedded app should:
 *   1. Initialise the Teams JS SDK and silently get the signed-in user's Entra
 *      SSO token via microsoftTeams.authentication.getAuthToken().
 *   2. POST that token to /api/me, where the backend validates it and maps the
 *      Entra identity to a CSM (manager) and/or the programme owner (sponsor).
 *   3. Outside Teams (a plain browser), fall back to the signed session cookie
 *      set by the simulated sign-in picker, or the configured default.
 *
 * The result decides which dashboard renders and which data is in scope.
 */
(function (global) {
  let _cache = null;
  const TEAMS_TIMEOUT_MS = 1500;

  // Complete the Teams tab load handshake as early as possible. On the new
  // `teams.cloud.microsoft` web client, a tab that doesn't call
  // `app.initialize()` (and notify it loaded) is flagged with "This app may have
  // issues in the web version of Teams". This runs immediately on script load,
  // independent of identity/SSO, so the host always gets the signal. In a plain
  // browser there's no Teams host, so `initialize()` simply never resolves and
  // this is a harmless no-op (it is fire-and-forget and never awaited).
  (function notifyTeamsLoaded() {
    try {
      const t = global.microsoftTeams;
      if (!t || !t.app || typeof t.app.initialize !== "function") return;
      t.app.initialize()
        .then(function () {
          try { if (typeof t.app.notifySuccess === "function") t.app.notifySuccess(); } catch (e) { /* ignore */ }
        })
        .catch(function () { /* not embedded in a Teams host — ignore */ });
    } catch (e) { /* ignore */ }
  })();

  function hasTeams() {
    return !!(global.microsoftTeams && global.microsoftTeams.authentication);
  }

  // Is this page actually hosted inside a Teams/M365 client? When opened as a
  // plain URL in a browser there is no host to answer the Teams JS handshake, so
  // app.initialize() would hang. We detect the embedded case (iframe or the
  // Teams query hints) and otherwise skip Teams entirely — no init, no dialog.
  function looksEmbedded() {
    try {
      const inIframe = global.self !== global.top;
      const q = (global.location.search || "").toLowerCase();
      const hint = q.includes("teams") || q.includes("subentityid") ||
                   q.includes("frame_context") || /teams\.|office\.|microsoft365\./.test(document.referrer || "");
      return inIframe || hint;
    } catch (e) {
      return true; // cross-origin access throwing means we're framed → embedded
    }
  }

  function withTimeout(promise, ms, label) {
    return Promise.race([
      promise,
      new Promise((_, reject) => setTimeout(() => reject(new Error((label || "timeout") + " after " + ms + "ms")), ms)),
    ]);
  }

  async function teamsToken() {
    // Only attempt Teams SSO when we're genuinely embedded; otherwise return
    // immediately so the browser demo "just works" (no init, no consent dialog).
    if (!hasTeams() || !looksEmbedded()) return null;
    try {
      await withTimeout(global.microsoftTeams.app.initialize(), TEAMS_TIMEOUT_MS, "teams.initialize");
      const token = await withTimeout(
        global.microsoftTeams.authentication.getAuthToken(), TEAMS_TIMEOUT_MS, "getAuthToken");
      return token || null;
    } catch (e) {
      console.info("Teams SSO unavailable; using browser session fallback:", e && e.message ? e.message : e);
      return null;
    }
  }

  async function whoami(force) {
    if (_cache && !force) return _cache;
    let token = null;
    try { token = await teamsToken(); } catch (e) { /* ignore */ }
    let res;
    if (token) {
      res = await fetch("/api/me", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token }),
      });
    } else {
      res = await fetch("/api/me", { headers: { Accept: "application/json" } });
    }
    _cache = await res.json();
    _cache.inTeams = hasTeams();
    return _cache;
  }

  async function signOut() {
    try { await fetch("/api/signout", { method: "POST" }); } catch (e) { /* ignore */ }
    _cache = null;
  }

  global.CSMIdentity = { whoami, signOut, hasTeams };
})(window);
