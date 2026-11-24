startup-begin = 插件加载中
startup-finish = 插件已就绪

prefs-title = PDF2zh

prefs-menu-translate = 翻译PDF
prefs-menu-cut = 裁剪PDF
prefs-menu-compare = 双语对照
prefs-menu-crop-compare = 双语对照(裁剪后拼接)

operation-progress-title = PDF2zh
local-service-waiting = 正在连接本机翻译服务，服务未运行时将自动启动…
local-service-wrong = 本机 8890 端口未返回 PDF2zh 健康状态，请检查占用该端口的服务。
local-service-missing = 找不到桌面快捷方式「Zotero PDF2zh 翻译服务.lnk」，请检查快捷方式或服务地址。
local-service-timeout = 已启动桌面快捷方式，但 90 秒内服务仍未就绪。请查看翻译服务窗口中的提示，然后重试。
operation-progress-processing = 正在准备请求...
operation-progress-submitting = 正在提交「{ $fileName }」...
operation-progress-accepted = 已提交，正在{ $action }「{ $fileName }」
operation-progress-running = 正在{ $action }「{ $fileName }」{ $suffix }
operation-progress-importing = 服务器已完成，正在导入「{ $fileName }」的附件...
operation-progress-file-done = 「{ $fileName }」已导入
operation-progress-file-failed = 「{ $fileName }」失败：{ $message }
operation-progress-waiting = 等待处理「{ $fileName }」
operation-progress-summary = 正在处理 { $done }/{ $total } 个文件
operation-action-translate = 翻译
operation-action-crop = 裁剪
operation-action-compare = 生成双语对照
operation-action-crop-compare = 生成裁剪双语对照
operation-batch-progress = 第 { $current }/{ $total } 个：{ $text }
operation-error-translate = “翻译 PDF”只支持原始 PDF。请选择原文附件或论文条目。
operation-error-crop = 当前文件不能再次裁剪。请选择原文、mono 或 dual 附件。
operation-error-compare = 当前文件不能执行“双语对照”。请选择原文或 dual 附件。
operation-error-crop-compare = 当前文件不能执行“双语对照（裁剪）”。请选择原文、dual 或 dual-cut 附件。
operation-error-crop-compare-terminal = 该 PDF 已经是“双语对照（裁剪）”结果，无需再次处理。请选择原文或 dual 附件。
operation-error-compare-terminal = 该 PDF 已经是“双语对照”结果，无需再次处理。请选择原文或 dual 附件。

operation-error-no-selection = 请先选择一个条目或 PDF 附件。
operation-error-unknown = 未知错误
operation-error-prefix = 错误：{ $message }
operation-error-single-file = 处理文件 { $fileName } 失败：{ $message }
operation-batch-started = 已开始处理 { $count } 个文件，请稍候
operation-batch-completed =
    { $kind ->
        [failed] 处理失败。失败 { $failed } 个
        [mixed] 处理结束。成功 { $succeeded } 个，失败 { $failed } 个
       *[success] 全部完成。成功 { $succeeded } 个
    }
operation-error-no-files = 服务器未返回可导入的文件
operation-error-completed-no-files = 任务已完成，但 Server 未返回任何文件
operation-error-partial-attachments = 仅导入 { $attached }/{ $total } 个附件：{ $message }
