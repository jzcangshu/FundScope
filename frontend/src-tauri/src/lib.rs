use tauri::AppHandle;
use tauri_plugin_dialog::DialogExt;

/// Show a native "save as" dialog for a browser-style download.
/// Returns the picked path, or None when the user cancels.
#[tauri::command]
async fn pick_download_destination(
    app: AppHandle,
    default_filename: String,
) -> Result<Option<String>, String> {
    // The blocking dialog variant must not run on the main thread; async
    // commands execute on the tokio worker pool, which is safe here.
    let file = app
        .dialog()
        .file()
        .set_file_name(&default_filename)
        .blocking_save_file();
    Ok(file.and_then(|file| file.into_path().ok()).map(|path| {
        path.to_string_lossy().into_owned()
    }))
}

/// Persist an already-downloaded export to the user-picked destination.
#[tauri::command]
fn write_saved_file(path: String, contents: Vec<u8>) -> Result<(), String> {
    std::fs::write(&path, contents).map_err(|error| error.to_string())
}

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_process::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .invoke_handler(tauri::generate_handler![
            pick_download_destination,
            write_saved_file
        ])
        .run(tauri::generate_context!())
        .expect("error while running fund dashboard desktop application");
}
