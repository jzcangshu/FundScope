import { createContext, useContext, useRef, useState, type ReactNode } from "react";
import { Form, Input, Modal } from "antd";

/* ---------- ConfirmReason: high-impact action confirmation with an audited reason ---------- */
export interface ConfirmOpts {
  title: string;
  description?: ReactNode;
  reasonLabel?: string;
  reasonRequired?: boolean;
  reasonPlaceholder?: string;
  danger?: boolean;
  okText?: string;
}

const ConfirmCtx = createContext<{ confirm: (opts: ConfirmOpts) => Promise<string | null> } | null>(null);

export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const [opts, setOpts] = useState<ConfirmOpts | null>(null);
  const [reason, setReason] = useState("");
  const resolver = useRef<(value: string | null) => void>(() => {});

  function confirm(next: ConfirmOpts): Promise<string | null> {
    setOpts(next);
    setReason("");
    setOpen(true);
    return new Promise((resolve) => {
      resolver.current = resolve;
    });
  }

  function close(value: string | null) {
    setOpen(false);
    resolver.current(value);
  }

  const trimmed = reason.trim();
  return (
    <ConfirmCtx.Provider value={{ confirm }}>
      {children}
      <Modal
        open={open}
        title={opts?.title}
        okText={opts?.okText ?? "确认"}
        okButtonProps={{ danger: opts?.danger, disabled: Boolean(opts?.reasonRequired) && !trimmed }}
        cancelText="取消"
        onCancel={() => close(null)}
        onOk={() => close(opts?.reasonRequired ? trimmed || null : trimmed)}
        destroyOnClose
      >
        {opts?.description && <p className="fd-caption" style={{ marginBottom: 12 }}>{opts.description}</p>}
        <Form layout="vertical">
          <Form.Item
            label={opts?.reasonLabel ?? "操作原因"}
            required={opts?.reasonRequired}
            extra="原因会随操作一并写入审计日志。"
            style={{ marginBottom: 0 }}
          >
            <Input.TextArea
              rows={3}
              value={reason}
              autoFocus
              onChange={(event) => setReason(event.target.value)}
              placeholder={opts?.reasonPlaceholder ?? "请填写原因"}
              maxLength={500}
              showCount
            />
          </Form.Item>
        </Form>
      </Modal>
    </ConfirmCtx.Provider>
  );
}

export function useConfirm() {
  const context = useContext(ConfirmCtx);
  if (!context) throw new Error("useConfirm outside provider");
  return context.confirm;
}
