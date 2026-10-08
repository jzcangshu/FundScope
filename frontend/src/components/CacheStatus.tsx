import { useEffect, useState } from "react";
import { Button, Tooltip } from "antd";
import { CloudOutlined, ReloadOutlined, WifiOutlined } from "@ant-design/icons";
import { clearReadCache } from "@/api/client";
import { isDesktopRuntime } from "@/app/runtime";

export function CacheStatus() {
  const [online, setOnline] = useState(() => typeof navigator === "undefined" || navigator.onLine);
  const [refreshing, setRefreshing] = useState(false);

  useEffect(() => {
    const onOnline = () => setOnline(true);
    const onOffline = () => setOnline(false);
    window.addEventListener("online", onOnline);
    window.addEventListener("offline", onOffline);
    return () => {
      window.removeEventListener("online", onOnline);
      window.removeEventListener("offline", onOffline);
    };
  }, []);

  async function refresh() {
    setRefreshing(true);
    await clearReadCache();
    window.location.reload();
  }

  const label = online ? "每次读取均校验当前账号的产品权限" : "当前离线，恢复连接后可重新加载已授权产品";
  return (
    <div className="fd-cache-status" aria-live="polite">
      <Tooltip title={label} placement="bottom">
        <span className={`fd-cache-status__indicator ${online ? "is-online" : "is-offline"}`}>
          {online ? <WifiOutlined /> : <CloudOutlined />}
          <span>{isDesktopRuntime() ? (online ? "在线" : "离线") : "实时"}</span>
        </span>
      </Tooltip>
      <Tooltip title="重新加载当前账号的授权数据">
        <Button
          type="text"
          size="small"
          icon={<ReloadOutlined spin={refreshing} />}
          aria-label="刷新数据"
          onClick={() => void refresh()}
          disabled={refreshing || !online}
        />
      </Tooltip>
    </div>
  );
}
