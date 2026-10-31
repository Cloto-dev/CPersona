// A service worker, so that the page can be installed as an app and opened from the
// desktop, the taskbar or the Dock. It keeps nothing: every request goes to the
// server as though there were no worker, so no memory and no page is stored in the
// browser for someone else at the same machine to read.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", event => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
