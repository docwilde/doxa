self.addEventListener('push', event => {
  event.waitUntil((async () => {
    let kind = 'update';
    try { kind = event.data?.json()?.kind || kind; } catch {}
    const windows = await self.clients.matchAll({type:'window', includeUncontrolled:true});
    if (windows.some(window => window.visibilityState === 'visible')) return;
    const title = kind === 'needs_input' ? 'DOXA needs input' : 'DOXA turn finished';
    await self.registration.showNotification(title, {
      body:'Open DOXA to review the session', tag:'doxa-remote',
      data:{url:self.registration.scope}
    });
  })());
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({type:'window', includeUncontrolled:true});
    const existing = windows.find(window => window.url.startsWith(self.registration.scope));
    if (existing) return existing.focus();
    return self.clients.openWindow(self.registration.scope);
  })());
});
