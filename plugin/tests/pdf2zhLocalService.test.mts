import assert from "node:assert/strict";
import test from "node:test";
import { ensureTranslationService } from "../src/modules/pdf2zhLocalService.ts";

const base = "http://127.0.0.1:8890";
const healthy = {
    status: 200,
    response: {
        status: "ok",
        version: "4.1.8",
        message: "PDF2zh Server is running",
    },
};
const offline = { status: 0 };

function host(
    responses = [healthy] as Array<{ status: number; response?: object }>,
) {
    const launched: string[] = [];
    const probes: string[] = [];
    const zotero = {
        isWin: true,
        HTTP: {
            TimeoutException: class extends Error {},
            request: async (method: string, url: string) => {
                assert.equal(method, "GET");
                probes.push(url);
                return responses.length > 1 ? responses.shift() : responses[0];
            },
        },
        launchFile: (path: string) => launched.push(path),
        Promise: { delay: async () => {} },
    };
    Object.assign(globalThis, {
        Zotero: zotero,
        Services: { dirsvc: { get: () => ({ path: "desktop" }) } },
        Components: { interfaces: { nsIFile: {} } },
        PathUtils: { join: (...parts: string[]) => parts.join("/") },
        IOUtils: { exists: async () => true },
    });
    const ensure = (url = base) =>
        ensureTranslationService(
            url,
            () => {},
            (id) => id,
        );
    return { ensure, launched, probes, zotero };
}

test("reuses a running service", async () => {
    const h = host();
    await h.ensure();
    assert.deepEqual(h.probes, [base + "/health"]);
    assert.deepEqual(h.launched, []);
});

test("a local connection timeout during cold start still launches the shortcut", async () => {
    const h = host();
    let calls = 0;
    h.zotero.HTTP.request = async () => {
        if (++calls === 1) throw new h.zotero.HTTP.TimeoutException();
        return healthy;
    };
    await h.ensure();
    assert.equal(calls, 2);
    assert.equal(h.launched.length, 1);
});

test("two translation batches wait for one shortcut launch and resume when ready", async () => {
    const h = host([offline, offline, healthy]);
    let resumed = 0;
    await Promise.all(
        [h.ensure(), h.ensure()].map((p) => p.then(() => resumed++)),
    );
    assert.equal(resumed, 2);
    assert.equal(h.probes.length, 3);
    assert.deepEqual(h.launched, ["desktop/Zotero PDF2zh 翻译服务.lnk"]);
});

test("a remote URL, different port or non-Windows host keeps its existing workflow", async () => {
    const h = host();
    for (const url of [
        "https://example.org",
        "http://127.0.0.1:8891",
        "http://localhost:8890/path",
        "http://localhost:8890@example.org",
    ])
        await h.ensure(url);
    h.zotero.isWin = false;
    await h.ensure();
    assert.deepEqual(h.probes, []);
    assert.deepEqual(h.launched, []);
});

test("an unrelated listener and a missing shortcut fail before submission", async () => {
    const h = host([
        { status: 200, response: { status: "ok", version: "other" } },
    ]);
    await assert.rejects(h.ensure(), /local-service-wrong/);
    assert.deepEqual(h.launched, []);
    const missing = host([offline]);
    Object.assign(globalThis, { IOUtils: { exists: async () => false } });
    await assert.rejects(missing.ensure(), /local-service-missing/);
    assert.deepEqual(missing.launched, []);
});

test("a failed startup times out and the next click can start again", async (t) => {
    let now = 0;
    t.mock.method(Date, "now", () => now);
    const replies = [offline];
    const h = host(replies);
    h.zotero.Promise.delay = async () => {
        now += 1000;
    };
    await assert.rejects(h.ensure(), /local-service-timeout/);
    replies.push(healthy);
    await h.ensure();
    assert.equal(h.launched.length, 2);
});
