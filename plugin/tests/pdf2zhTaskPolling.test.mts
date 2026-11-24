import assert from "node:assert/strict";
import test from "node:test";
import { visibleTask } from "../src/modules/pdf2zhTaskPolling.ts";
import { closePDF2zhProgressWindows } from "../src/modules/pdf2zhProgressWindows.ts";

test("hot reload closes only native progress windows owned by PDF2zh", () => {
    const closed: string[] = [];
    const removed: unknown[] = [];
    function win(uri: string, title: string) {
        return {
            document: {
                documentURI: uri,
                querySelector: () => ({ getAttribute: () => title }),
            },
            close: () => closed.push(title),
        };
    }
    const uri = "chrome://zotero/content/progressWindow.xhtml";
    const ours = win(uri, "PDF2zh");
    const remaining = [
        ours,
        win(uri, "Other plugin"),
        win("about:blank", "PDF2zh"),
    ];
    Object.assign(globalThis, {
        Services: {
            ww: {
                getWindowEnumerator: () => ({
                    hasMoreElements: () => remaining.length > 0,
                    getNext: () => remaining.shift(),
                }),
            },
        },
        Zotero: {
            ProgressWindowSet: { remove: (w: unknown) => removed.push(w) },
        },
    });
    closePDF2zhProgressWindows();
    assert.deepEqual(closed, ["PDF2zh"]);
    assert.deepEqual(removed, [ours]);
});

function harness(
    read: () => Promise<Record<string, unknown> | undefined>,
    exact = true,
) {
    let elapsed = 0;
    const notices: string[] = [];
    const run = () =>
        visibleTask(
            read,
            exact,
            (s) => notices.push(s),
            (s) => s,
            async (ms) => {
                elapsed += ms;
            },
            () => elapsed,
        );
    return { run, notices, elapsed: () => elapsed };
}

test("a stopped server reaches a terminal error without resubmitting the PDF", async () => {
    const h = harness(async () => {
        throw new Error("refused");
    });
    await assert.rejects(h.run(), /offline/);
    assert.equal(h.elapsed(), 15_000);
    assert.ok(h.notices.length > 0);
});

test("a restarted server reports a lost accepted task immediately", async () => {
    const h = harness(async () => undefined);
    await assert.rejects(h.run(), /missing/);
    assert.equal(h.elapsed(), 0);
});

test("a transient outage or delayed legacy history can recover", async () => {
    let calls = 0;
    const h = harness(async () => {
        if (++calls === 1) throw new Error("offline");
        if (calls === 2) return undefined;
        return { status: "queued", progress: 0 };
    }, false);
    assert.deepEqual(await h.run(), { status: "queued", progress: 0 });
    assert.deepEqual(h.notices, ["offline", "missing"]);
});
