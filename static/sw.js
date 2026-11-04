self.addEventListener('push', function(event) {
    if (!event.data) return;
    try {
        const payload = event.data.json();
        const title = payload.title || 'IDC Trading';
        const options = {
            body: payload.body || 'Trade signal executed.',
            icon: '/static/apple-touch-icon.png',
            badge: '/static/apple-touch-icon.png',
            vibrate: [200, 100, 200],
            data: { url: payload.url || '/' }
        };
        event.waitUntil(self.registration.showNotification(title, options));
    } catch (e) {
        event.waitUntil(self.registration.showNotification('IDC Trading', {
            body: event.data.text(),
            icon: '/static/apple-touch-icon.png'
        }));
    }
});

self.addEventListener('notificationclick', function(event) {
    event.notification.close();
    event.waitUntil(
        clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function(clientList) {
            for (let i = 0; i < clientList.length; i++) {
                let client = clientList[i];
                if (client.url === '/' && 'focus' in client) {
                    return client.focus();
                }
            }
            if (clients.openWindow) {
                return clients.openWindow('/');
            }
        })
    );
});