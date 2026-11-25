(() => {
  function redirectAuth() {
    if (location.pathname !== "/") return;
    const query = new URLSearchParams(location.search);
    const screen = query.get("screen");
    if (screen !== "auth-login" && screen !== "auth-register") return;
    if (query.has("invitation-token")) return;
    const destination = screen === "auth-register" ? "/auth/register" : "/auth/login";
    const target = new URL(destination, location.origin);
    const callback = query.get("callback-url");
    if (callback) {
      try {
        const url = new URL(callback, location.origin);
        if (url.origin === location.origin && url.pathname === "/") {
          target.searchParams.set("next", url.pathname + url.search);
        }
      } catch {
        // An invalid callback can fall through to the dashboard.
      }
    }
    location.replace(target);
  }
  for (const method of ["pushState", "replaceState"]) {
    const original = history[method];
    history[method] = function (...args) {
      const result = original.apply(this, args);
      queueMicrotask(redirectAuth);
      return result;
    };
  }
  addEventListener("popstate", redirectAuth);
  redirectAuth();
})();
