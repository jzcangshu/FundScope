// 在 release 模式下把子系统设为 windows，避免桌面应用启动时弹出黑色命令行窗口。
// 这个属性必须放在包含 main 函数的 binary crate（本文件）上；放在 lib.rs 上无效。
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    fund_dashboard_lib::run();
}
