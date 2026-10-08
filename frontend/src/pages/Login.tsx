import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Button, Checkbox, Form, Input } from "antd";
import { useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "@/app/auth";
import { isDesktopRuntime } from "@/app/runtime";
import { BrandMark, PRODUCT_NAME, PRODUCT_TAGLINE, ThemeToggle } from "@/app/layout";
import { useToast } from "@/components";
import { apiErrorMessage } from "@/api/client";

const REMEMBER_KEY = "fund-dashboard.remember-credentials";
const REMEMBER_USERNAME_KEY = "fund-dashboard.remember-username";

const POINTS = ["智能归档", "可信发布", "全程可溯"];

const HEADLINE = (
  <>
    每日估值
    <br />
    一眼掌握<em>全局</em>
  </>
);
const SUBHEAD = "数据规模到风险收益，一张视图清晰呈现";

/**
 * Cover-page ambience: one warm glow follows the pointer across the whole page
 * (eased), and an ink "lens" follows it inside the headline. The lens is a
 * clipped, inverted copy of the headline (`clip-path: circle()` driven by CSS
 * variables), so it needs no blend modes and looks identical in both themes.
 * Everything is transforms / custom properties on refs — no React re-render
 * on pointer move.
 */
export function useCoverPointer() {
  const rootRef = useRef<HTMLDivElement>(null);
  const heroRef = useRef<HTMLDivElement>(null);
  const glowRef = useRef<HTMLDivElement>(null);
  const [hasPointer, setHasPointer] = useState(false);

  useEffect(() => {
    const root = rootRef.current;
    const hero = heroRef.current;
    if (!root || !hero) return;
    const fine = window.matchMedia("(hover: hover) and (pointer: fine)").matches;
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (!fine || reduced) return;

    let target = { x: -1000, y: -1000 };
    let current = { x: -1000, y: -1000 };
    let frame = 0;

    const tick = () => {
      current = { x: current.x + (target.x - current.x) * 0.14, y: current.y + (target.y - current.y) * 0.14 };
      if (glowRef.current) glowRef.current.style.transform = `translate3d(${current.x}px, ${current.y}px, 0)`;
      const box = hero.getBoundingClientRect();
      hero.style.setProperty("--lx", `${current.x - box.left}px`);
      hero.style.setProperty("--ly", `${current.y - box.top}px`);
      const settled = Math.abs(target.x - current.x) < 0.3 && Math.abs(target.y - current.y) < 0.3;
      frame = settled ? 0 : requestAnimationFrame(tick);
    };
    const move = (event: PointerEvent) => {
      target = { x: event.clientX, y: event.clientY };
      setHasPointer(true);
      if (!frame) frame = requestAnimationFrame(tick);
    };
    const lensOn = () => hero.classList.add("is-lensed");
    const lensOff = () => hero.classList.remove("is-lensed");
    root.addEventListener("pointermove", move);
    hero.addEventListener("pointerenter", lensOn);
    hero.addEventListener("pointerleave", lensOff);
    return () => {
      root.removeEventListener("pointermove", move);
      hero.removeEventListener("pointerenter", lensOn);
      hero.removeEventListener("pointerleave", lensOff);
      if (frame) cancelAnimationFrame(frame);
    };
  }, []);

  return { rootRef, heroRef, glowRef, hasPointer };
}

/** Background layers shared by the login and first-time-setup covers. */
export function CoverAmbient({ glowRef }: { glowRef?: React.RefObject<HTMLDivElement> }) {
  return (
    <div className="fd-login__ambient" aria-hidden="true">
      <div className="fd-login__blob fd-login__blob--peach" />
      <div className="fd-login__blob fd-login__blob--mist" />
      <div className="fd-login__blob fd-login__blob--warm" />
      <div className="fd-login__grid" />
      {glowRef && <div className="fd-login__glow" ref={glowRef} />}
    </div>
  );
}

/**
 * Silk: the card's vermilion header. A gradient with a few soft highlights and
 * shadows, displaced by fractal noise so it folds like cloth. Pure SVG, no
 * image assets, and it is the only surface allowed to be brand-coloured.
 * All colours come from `--silk-*` custom properties so each theme can set its
 * own weave (daylight vermilion vs. a deeper, lacquered night red).
 */
const silk = (token: string) => ({ style: { fill: `var(--silk-${token})` } });

export function Silk({ children }: { children: React.ReactNode }) {
  return (
    <div className="fd-silk">
      <svg viewBox="0 0 440 300" preserveAspectRatio="none" aria-hidden="true" focusable="false">
        <defs>
          <linearGradient id="fd-silk-base" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0" style={{ stopColor: "var(--silk-base-1)" }} />
            <stop offset="0.55" style={{ stopColor: "var(--silk-base-2)" }} />
            <stop offset="1" style={{ stopColor: "var(--silk-base-3)" }} />
          </linearGradient>
          <filter id="fd-silk-fold" x="-30%" y="-30%" width="160%" height="160%" colorInterpolationFilters="sRGB">
            <feTurbulence type="fractalNoise" baseFrequency="0.004 0.012" numOctaves="3" seed="7" result="noise" />
            <feGaussianBlur in="SourceGraphic" stdDeviation="9" result="soft" />
            <feDisplacementMap in="soft" in2="noise" scale="170" xChannelSelector="R" yChannelSelector="G" />
          </filter>
        </defs>
        <rect width="440" height="300" fill="url(#fd-silk-base)" />
        <g filter="url(#fd-silk-fold)">
          <ellipse cx="120" cy="60" rx="190" ry="40" opacity="0.85" {...silk("hi-1")} />
          <ellipse cx="340" cy="110" rx="140" ry="22" opacity="0.7" {...silk("hi-2")} />
          <ellipse cx="240" cy="180" rx="230" ry="30" opacity="0.8" {...silk("lo-1")} />
          <ellipse cx="80" cy="250" rx="150" ry="28" opacity="0.7" {...silk("hi-3")} />
          <ellipse cx="300" cy="270" rx="190" ry="34" opacity="0.7" {...silk("lo-2")} />
          <ellipse cx="410" cy="20" rx="130" ry="30" opacity="0.65" {...silk("lo-3")} />
          <ellipse cx="200" cy="120" rx="90" ry="10" opacity="0.6" {...silk("hi-4")} />
        </g>
      </svg>
      {children}
    </div>
  );
}

export default function Login() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const toast = useToast();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [form] = Form.useForm<{ username: string; password: string; remember: boolean }>();
  const from = (location.state as { from?: string } | null)?.from;
  const target = from && from.startsWith("/") && !from.startsWith("/login") ? from : "/dashboard";
  const { rootRef, heroRef, glowRef, hasPointer } = useCoverPointer();
  // 「记住密码」只在桌面客户端里启用；浏览器版最多记住账号（不存密码到共享浏览器）。
  const desktop = isDesktopRuntime();

  useEffect(() => {
    try {
      const username = window.localStorage.getItem(REMEMBER_USERNAME_KEY);
      const rememberRaw = window.localStorage.getItem(REMEMBER_KEY);
      const remember = rememberRaw === "true";
      if (username) form.setFieldValue("username", username);
      if (remember && desktop) {
        const password = window.localStorage.getItem(REMEMBER_KEY.replace("remember-credentials", "remember-password"));
        if (password) form.setFieldValue("password", password);
      }
      form.setFieldValue("remember", remember);
    } catch {
      // localStorage 不可用时静默跳过。
    }
  }, [desktop, form]);

  const onLogin = useCallback(
    async (values: { username: string; password: string; remember?: boolean }) => {
      setLoading(true);
      setError(null);
      try {
        const session = await login(values.username.trim(), values.password);
        const wantRemember = values.remember === true;
        try {
          if (values.username.trim()) window.localStorage.setItem(REMEMBER_USERNAME_KEY, values.username.trim());
          if (desktop && wantRemember) {
            window.localStorage.setItem(REMEMBER_KEY, "true");
            window.localStorage.setItem("fund-dashboard.remember-password", values.password);
          } else {
            window.localStorage.setItem(REMEMBER_KEY, "false");
            window.localStorage.removeItem("fund-dashboard.remember-password");
          }
        } catch {
          // 存储不可用时忽略。
        }
        toast.success(`欢迎回来，${session.display_name?.trim() || session.username}`);
        navigate(target, { replace: true });
      } catch (cause) {
        setError(apiErrorMessage(cause, "登录失败，请稍后重试"));
      } finally {
        setLoading(false);
      }
    },
    [desktop, login, navigate, target, toast],
  );

  return (
    <div className={`fd-login ${hasPointer ? "has-pointer" : ""}`.trim()} ref={rootRef}>
      <CoverAmbient glowRef={glowRef} />
      <ThemeToggle className="fd-login__theme" />
      <section className="fd-login__showcase" aria-label="产品介绍">
        <div className="fd-login__brand">
          <BrandMark size="lg" />
          <div>
            <strong>{PRODUCT_NAME}</strong>
            <span>{PRODUCT_TAGLINE}</span>
          </div>
        </div>
        <div className="fd-login__hero" ref={heroRef}>
          <h1>{HEADLINE}</h1>
          <div className="fd-login__lens" aria-hidden="true">
            <div className="fd-login__lens-disc" />
            <h1>{HEADLINE}</h1>
          </div>
          <p>{SUBHEAD}</p>
        </div>
        <ul className="fd-login__points">
          {POINTS.map((point) => <li key={point}>{point}</li>)}
        </ul>
      </section>
      <section className="fd-login__card" aria-label="登录">
        <Silk>
          <BrandMark />
          <h2 className="fd-silk__title">登录</h2>
          <p className="fd-silk__sub">使用内部账号继续</p>
        </Silk>
        <div className="fd-login__form">
          {error && <Alert type="error" showIcon className="fd-alert" message={error} />}
          <Form form={form} layout="vertical" onFinish={onLogin} requiredMark={false} size="large">
            <Form.Item label="账号" name="username" rules={[{ required: true, message: "请输入账号" }]}>
              <Input autoComplete="username" placeholder="账号" autoFocus />
            </Form.Item>
            <Form.Item label="密码" name="password" rules={[{ required: true, message: "请输入密码" }]}>
              <Input.Password autoComplete="current-password" placeholder="密码" />
            </Form.Item>
            <Form.Item name="remember" valuePropName="checked" style={{ marginBottom: 16 }}>
              <Checkbox>记住密码{desktop ? "" : "（仅记住账号）"}</Checkbox>
            </Form.Item>
            <Button type="primary" htmlType="submit" block loading={loading}>
              登录
            </Button>
          </Form>
          <p className="fd-login__security">{desktop ? "桌面客户端仅在本机保存凭据。" : "浏览器不保存敏感信息。"}忘记密码请联系系统管理员。</p>
        </div>
      </section>
    </div>
  );
}
