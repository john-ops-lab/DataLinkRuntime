/** 登录页：品牌区 + Token 登录卡片（M3.1 §6，认证合同仍完全沿用 M2）。 */

import { useState } from "react";
import { Button, Card, Input } from "antd";

import { ApiError } from "../api";

interface LoginPageProps {
  notice: string | null;
  onSubmit: (token: string) => Promise<void>;
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return `${error.message} (${error.code})`;
  }
  return "请求失败";
}

// 品牌区特性卡片只做产品定位说明（来自 docs/product.md），不代表新功能入口。
const BRAND_FEATURES = [
  { title: "轻量高效", text: "轻量内核，快速集成" },
  { title: "多源适配", text: "丰富连接，灵活适配" },
  { title: "安全可控", text: "Token 认证，权限可控" },
  { title: "稳定可靠", text: "组件精简，可观测内建" },
];

export default function LoginPage(props: LoginPageProps) {
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function handleSubmit() {
    if (busy || !token.trim()) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await props.onSubmit(token.trim());
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="login-page">
      <section className="login-brand">
        <div>
          <h1 className="login-brand-logo">DLR</h1>
          <div className="login-brand-name">DataLinkRuntime</div>
        </div>
        <p className="login-brand-tagline">轻量数据适配运行平台</p>
        <p className="login-brand-sub">
          Adapter 在线编辑、保存版本、测试运行、实时日志与执行记录，全部在浏览器中完成；
          一台服务器 + Docker Compose 即可运行完整平台。
        </p>
        <div className="login-features">
          {BRAND_FEATURES.map((feature) => (
            <div className="login-feature" key={feature.title}>
              <span className="login-feature-title">{feature.title}</span>
              <span className="login-feature-text">{feature.text}</span>
            </div>
          ))}
        </div>
        <p className="login-copyright">© DataLinkRuntime (DLR) · 轻量 · 连接 · 适配 · 运行</p>
      </section>

      <section className="login-side">
        <Card className="login-card">
          <div className="login-card-inner">
            <h2 className="login-card-title">欢迎登录 DLR 控制台</h2>
            <p className="login-card-subtitle">请输入管理员 Token 以继续</p>
            {props.notice && (
              <p className="login-notice" data-testid="auth-notice">
                {props.notice}
              </p>
            )}
            {error && (
              <p className="error-banner" role="alert" data-testid="login-error">
                {error}
              </p>
            )}
            <Input.Password
              data-testid="admin-token-input"
              placeholder="请输入管理员 Token"
              value={token}
              disabled={busy}
              onChange={(event) => setToken(event.target.value)}
              onPressEnter={() => void handleSubmit()}
            />
            <Button
              type="primary"
              block
              data-testid="admin-token-submit"
              loading={busy}
              disabled={busy || !token.trim()}
              onClick={() => void handleSubmit()}
            >
              登录
            </Button>
            <p className="login-card-subtitle" style={{ margin: 0 }}>
              Token 仅保存在当前浏览器会话中，不写入数据库。
            </p>
          </div>
        </Card>
      </section>
    </main>
  );
}
