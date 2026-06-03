import { Button, Card, Descriptions, Typography } from "antd";
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";

import { api, clearToken } from "../api/client";

type Me = {
  id: number;
  username: string;
  role: string;
  created_at: string;
};

export default function AppPlaceholder() {
  const navigate = useNavigate();
  const [me, setMe] = useState<Me | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .get("/v1/auth/me")
      .then((resp) => {
        if (!cancelled) setMe(resp.data);
      })
      .catch((e) => {
        if (!cancelled) setError(e?.response?.data?.detail ?? "加载失败");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div style={{ maxWidth: 800, margin: "64px auto", padding: 16 }}>
      <Card>
        <Typography.Title level={3} style={{ marginTop: 0 }}>
          /app
        </Typography.Title>

        {error ? (
          <Typography.Paragraph type="danger">{String(error)}</Typography.Paragraph>
        ) : null}

        {me ? (
          <Descriptions bordered size="small" column={1}>
            <Descriptions.Item label="username">{me.username}</Descriptions.Item>
            <Descriptions.Item label="role">{me.role}</Descriptions.Item>
          </Descriptions>
        ) : (
          <Typography.Paragraph type="secondary">加载中…</Typography.Paragraph>
        )}

        <div style={{ marginTop: 16, display: "flex", gap: 8 }}>
          <Button
            onClick={() => {
              clearToken();
              navigate("/login", { replace: true });
            }}
          >
            退出登录（清 token）
          </Button>
        </div>
      </Card>
    </div>
  );
}

