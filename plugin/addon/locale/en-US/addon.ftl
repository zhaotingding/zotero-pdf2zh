startup-begin = Addon is loading
startup-finish = Addon is ready

prefs-title = PDF2zh

prefs-menu-translate = Translate PDF
prefs-menu-cut = Cut PDF
prefs-menu-crop-compare = Bilingual PDF(After Cropping)
prefs-menu-compare = Bilingual PDF

operation-progress-title = PDF2zh
local-service-waiting = Connecting to the local translation service and starting it if needed…
local-service-wrong = Port 8890 did not return a healthy PDF2zh service. Check the service using this port.
local-service-missing = The desktop shortcut “Zotero PDF2zh 翻译服务.lnk” was not found. Check the shortcut or server address.
local-service-timeout = The shortcut was launched, but the service was not ready within 90 seconds. Check the service window and try again.
operation-progress-processing = Preparing request...
operation-progress-submitting = Submitting “{ $fileName }”...
operation-progress-accepted = Submitted. { $action } “{ $fileName }” now
operation-progress-running = { $action } “{ $fileName }”{ $suffix }
operation-progress-importing = Server finished. Importing attachments for “{ $fileName }”...
operation-progress-file-done = Imported “{ $fileName }”
operation-progress-file-failed = “{ $fileName }” failed: { $message }
operation-progress-waiting = Waiting to process “{ $fileName }”
operation-progress-summary = Processing { $done }/{ $total } file(s)
operation-action-translate = Translating
operation-action-crop = Cropping
operation-action-compare = Building bilingual PDF
operation-action-crop-compare = Building cropped bilingual PDF
operation-batch-progress = { $current }/{ $total }: { $text }
operation-error-translate = “Translate PDF” only accepts an original PDF. Select the original attachment or its parent item.
operation-error-crop = This file cannot be cropped again. Select an original, mono, or dual attachment.
operation-error-compare = This file cannot be used for Bilingual Compare. Select an original or dual attachment.
operation-error-crop-compare = This file cannot be used for Cropped Bilingual Compare. Select an original, dual, or dual-cut attachment.
operation-error-crop-compare-terminal = This PDF is already a Cropped Bilingual Compare result. Select the original or a dual attachment instead.
operation-error-compare-terminal = This PDF is already a Bilingual Compare result. Select the original or a dual attachment instead.

operation-error-no-selection = Select an item or PDF attachment first.
operation-error-unknown = Unknown error
operation-error-prefix = Error: { $message }
operation-error-single-file = Failed to process { $fileName }: { $message }
operation-batch-started = Started { $count } file(s). Please wait.
operation-batch-completed =
    { $kind ->
        [failed] Failed. { $failed } file(s) failed
        [mixed] Finished. Succeeded: { $succeeded }, failed: { $failed }
       *[success] All done. Succeeded: { $succeeded }
    }
operation-error-no-files = The server did not return any files to import.
operation-error-completed-no-files = The task completed, but the Server returned no files.
operation-error-partial-attachments = Imported only { $attached }/{ $total } attachment(s): { $message }

task-reconnecting = Contact with the task was lost. Reconnecting…
task-offline = The translation server is offline. Check the service before translating the original PDF again.
task-missing = The server no longer has this task, possibly after a restart. Start again from the original PDF.
task-already-running = This attachment is already being processed.
