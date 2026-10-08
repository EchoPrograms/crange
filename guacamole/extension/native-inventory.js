(function (root) {
    'use strict';

    function connectionUrl(clientIdentifier) {
        return '#/client/' + encodeURIComponent(clientIdentifier);
    }

    function verifiedEntries(catalog, inventory) {
        if (!inventory) return [];
        return catalog.reduce(function (verified, item) {
            var metadata = inventory[String(item.source) + '\u0000' + String(item.connection.identifier)];
            if (!metadata || metadata.guest_matched !== true || !metadata.hostname) return verified;
            verified.push({source: item.source, connection: item.connection,
                url: item.url, metadata: metadata});
            return verified;
        }, []);
    }

    var api = {connectionUrl: connectionUrl, verifiedEntries: verifiedEntries};
    root.CRangeNativeInventory = api;
    if (typeof module === 'object' && module.exports) module.exports = api;
}(typeof window === 'undefined' ? globalThis : window));
