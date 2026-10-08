import { useState } from "react";
import { Alert, Button, Form, Input, Progress } from "antd";
import { useNavigate } from "react-router-dom";
import { useToast } from "@/components";
import { useAuth } from "@/app/auth";
import { BrandMark, PRODUCT_NAME, PRODUCT_TAGLINE, ThemeToggle } from "@/app/layout";
import { apiErrorMessage, isApiError } from "@/api/client";
import { CoverAmbient, Silk } from "./Login";

function passwordStrength(value: string): number {
  let score = 0;
  if (value.length >= 8) score += 25;
  if (/[A-Z]/.test(value)) score += 25;
  if (/[0-9]/.test(value)) score += 25;
  if (/[^A-Za-z0-9]/.test(value)) score += 25;
  return score;
}

export default function Initialize() {
  const navigate = useNavigate();
  const toast = useToast();
  const { initialize, refresh } = useAuth();
  const [loading, setLoading] = useState(false);
  const [strength, setStrength] = useState(0);
  const [error, setError] = useState<string | null>(null);

  async function onFinish(values: { username: string; password: string; display_name?: string }) {
    setLoading(true);
    setError(null);
    try {
      await initialize({
        username: values.username.trim(),
        password: values.password,
        display_name: values.display_name?.trim() || undefined,
      });
      toast.success("管理员创建成功");
      navigate("/dashboard", { replace: true });
    } catch (cause) {
      if (isApiError(cause, 409)) {
        await refresh().catch(() => null);
        navigate("/login", { replace: true });
        return;
      }
      setError(apiErrorMessage(cause, "初始化失败，请稍后重试"));
    } finally {
      setLoading(false);
    }
  }

  const strengthColor = strength < 50 ? "var(--danger)" : strength < 75 ? "var(--warning)" : "var(--success)";

  return (
    <div className="fd-login fd-login--setup">
      <CoverAmbient />
      <ThemeToggle className="fd-login__theme" />
      <div className="fd-login__card">
        <Silk>
          <BrandMark />
          <h1 className="fd-silk__title">系统初始化</h1>
          <p className="fd-silk__sub">{PRODUCT_NAME} · {PRODUCT_TAGLINE}</p>
        </Silk>
        <div className="fd-login__form">
        <p className="fd-login__subtitle">系统尚未创建任何账号。请创建首个系统管理员，完成后此页面将永久关闭。</p>
        {error && <Alert type="error" showIcon className="fd-alert" message={error} />}
        <Form layout="vertical" onFinish={onFinish} requiredMark={false} size="large">
          <Form.Item label="管理员账号" name="username" rules={[{ required: true, message: "请输入账号" }]}>
            <Input placeholder="例如 admin" autoComplete="username" autoFocus />
          </Form.Item>
          <Form.Item label="显示名称" name="display_name">
            <Input placeholder="例如 系统管理员" />
          </Form.Item>
          <Form.Item
            label="密码"
            name="password"
            rules={[
              { required: true, message: "请输入密码" },
              { min: 8, message: "密码至少 8 位" },
              {
                validator: (_, value) =>
                  value && /[A-Z]/.test(value) && /[0-9]/.test(value)
                    ? Promise.resolve()
                    : Promise.reject(new Error("需包含大写字母和数字")),
              },
            ]}
            extra={strength > 0 && (
              <Progress percent={strength} size="small" showInfo={false} strokeColor={strengthColor} style={{ marginTop: 6 }} aria-label="密码强度" />
            )}
          >
            <Input.Password autoComplete="new-password" placeholder="至少 8 位，含大写字母和数字" onChange={(event) => setStrength(passwordStrength(event.target.value))} />
          </Form.Item>
          <Form.Item
            label="确认密码"
            name="confirm"
            dependencies={["password"]}
            rules={[
              { required: true, message: "请再次输入密码" },
              ({ getFieldValue }) => ({
                validator(_, value) {
                  return value === getFieldValue("password") ? Promise.resolve() : Promise.reject(new Error("两次密码不一致"));
                },
              }),
            ]}
          >
            <Input.Password autoComplete="new-password" />
          </Form.Item>
          <Button type="primary" htmlType="submit" block loading={loading}>
            创建管理员并登录
          </Button>
        </Form>
        </div>
      </div>
    </div>
  );
}