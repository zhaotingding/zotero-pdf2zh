// A hot update can leave native Zotero windows from the previous plugin instance alive.
export function closePDF2zhProgressWindows() {
    const windows = Services.ww.getWindowEnumerator();
    const owned: Window[] = [];
    while (windows.hasMoreElements()) {
        const win = windows.getNext() as unknown as Window;
        if (
            win.document?.documentURI !==
            "chrome://zotero/content/progressWindow.xhtml"
        )
            continue;
        const headline = win.document.querySelector(
            "#zotero-progress-text-headline > label",
        );
        if (headline?.getAttribute("value") === "PDF2zh") owned.push(win);
    }
    const registry = Zotero as typeof Zotero & {
        ProgressWindowSet: { remove(win: Window): void };
    };
    for (const win of owned) {
        registry.ProgressWindowSet.remove(win);
        win.close();
    }
}
