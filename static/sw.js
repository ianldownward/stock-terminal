// IDC Trading Service Worker - Push & Offline Engine

self.addEventListener('install', (event) => {
    self.skipWaiting();
});

self.addEventListener('activate', (event) => {
    event.waitUntil(clients.claim());
});

// NATIVE APPLE WEB PUSH HANDLER
self.addEventListener('push', (event) => {
    let data = {
        title: 'IDC Trading Alert',
        body: 'New live trade executed.',
        url: '/'
    };

    if (event.data) {
        try {
            data = event.data.json();
        } catch(e) {
            data.body = event.data.text();
        }
    }

    const options = {
        body: data.body,
        icon: '/static/apple-touch-icon.png',
        badge: '/static/apple-touch-icon.png',
        data: { url: data.url || '/' },
        vibrate: [200, 100, 200],
        tag: 'trade-alert-' + Date.now(),
        renotify: true
    };

    event.waitUntil(
        self.registration.showNotification(data.title, options)
    );
});

// CLICK TO OPEN TERMINAL
self.addEventListener('notificationclick', (event) => {
    event.notification.close();
    event.waitUntil(
        clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clientList) => {
            for (let i = 0; i < clientList.length; i++) {
                let client = clientList[i];
                if (client.url.includes(self.location.origin) && 'focus' in client) {
                    return client.focus();
                }
            }
            if (clients.openWindow) {
                return clients.openWindow(event.notification.data.url || '/');
            }
        })
    );
});