type Message = (
    key:
        | "local-service-wrong"
        | "local-service-missing"
        | "local-service-timeout",
) => string;

// 同一 Zotero 进程中的多个阅读窗口和批次共用一次启动。
let starting: Promise<void> | undefined;

async function ready(base: string, message: Message): Promise<boolean> {
    let response: XMLHttpRequest;
    try {
        response = await Zotero.HTTP.request("GET", base + "/health", {
            timeout: 2000,
            responseType: "json",
            noCache: true,
            followRedirects: false,
            successCodes: false,
            errorDelayMax: 0,
        });
    } catch (error) {
        // Windows 的回环连接在端口尚未监听时也可能先触发 XHR 超时。
        const { TimeoutException } = Zotero.HTTP as typeof Zotero.HTTP & {
            TimeoutException: new (ms: number) => Error;
        };
        if (
            (error as { status?: number })?.status === 0 ||
            error instanceof TimeoutException
        )
            return false;
        throw error;
    }
    if (response.status === 0) return false;
    if (
        response.status !== 200 ||
        response.response?.status !== "ok" ||
        response.response?.message !== "PDF2zh Server is running" ||
        typeof response.response?.version !== "string"
    ) {
        throw new Error(message("local-service-wrong"));
    }
    return true;
}

async function startIfNeeded(base: string, message: Message) {
    if (await ready(base, message)) return;
    // 复用用户的桌面快捷方式，让工作区、解释器和凭据始终由同一个入口决定。
    const desktop = Services.dirsvc.get("Desk", Components.interfaces.nsIFile);
    const shortcut = PathUtils.join(desktop.path, "Zotero PDF2zh 翻译服务.lnk");
    if (!(await IOUtils.exists(shortcut))) {
        throw new Error(message("local-service-missing"));
    }
    Zotero.launchFile(shortcut);
    const deadline = Date.now() + 90_000;
    do {
        await Zotero.Promise.delay(500);
        if (await ready(base, message)) return;
    } while (Date.now() < deadline);
    throw new Error(message("local-service-timeout"));
}

export async function ensureTranslationService(
    url: string,
    waiting: () => void,
    message: Message,
): Promise<void> {
    const base = url.trim().replace(/\/+$/, "");
    if (
        !Zotero.isWin ||
        !/^http:\/\/(localhost|127\.0\.0\.1):8890$/i.test(base)
    )
        return;
    waiting();
    if (!starting) {
        starting = startIfNeeded(base, message).finally(() => {
            starting = undefined;
        });
    }
    await starting;
}
