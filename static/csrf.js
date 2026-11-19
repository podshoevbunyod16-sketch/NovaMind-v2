/* Adds the synchronizer token cookie as a header to same-origin mutations. */
(() => {
  const originalFetch = window.fetch.bind(window);
  const safeMethods = new Set(["GET", "HEAD", "OPTIONS", "TRACE"]);

  function readTokenCookie() {
    const entry = document.cookie.split(";").map((part) => part.trim())
      .find((part) => part.startsWith("nova_csrf="));
    if (!entry) return "";
    try {
      return decodeURIComponent(entry.slice("nova_csrf=".length));
    } catch (_error) {
      return "";
    }
  }

  window.fetch = function csrfAwareFetch(input, init) {
    const options = init || {};
    const requestObject = typeof Request !== "undefined" && input instanceof Request;
    const method = String(options.method || (requestObject ? input.method : "GET")).toUpperCase();
    if (safeMethods.has(method)) return originalFetch(input, init);

    let target;
    try {
      target = new URL(requestObject ? input.url : input, window.location.href);
    } catch (_error) {
      return originalFetch(input, init);
    }
    if (target.origin !== window.location.origin) return originalFetch(input, init);

    const headers = new Headers(requestObject ? input.headers : undefined);
    new Headers(options.headers || undefined).forEach((value, key) => headers.set(key, value));
    const token = readTokenCookie();
    if (token) headers.set("X-CSRF-Token", token);
    return originalFetch(input, { ...options, headers });
  };
})();
