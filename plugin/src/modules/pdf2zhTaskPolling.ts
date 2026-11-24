type Task = Record<string, unknown>;
type State = "offline" | "missing";

// An accepted task may run for hours. Only loss of contact / loss of its ID has a short deadline.
export async function visibleTask(
    read: () => Promise<Task | undefined>,
    exactStatus: boolean,
    notice: (state: State) => void,
    failure: (state: State) => string,
    delay: (ms: number) => Promise<unknown>,
    now = Date.now,
): Promise<Task> {
    const deadline = now() + 15_000;
    for (;;) {
        let state: State = "missing";
        try {
            const task = await read();
            if (task) return task;
        } catch {
            state = "offline";
        }
        if ((state === "missing" && exactStatus) || now() >= deadline) {
            throw new Error(failure(state));
        }
        notice(state);
        await delay(1000);
    }
}
