import { Button, Card, Form, Input, Typography, message } from "antd";
import { useNavigate } from "react-router-dom";

import { api } from "../api/client";

type RegisterForm = {
  username: string;
  password: string;
};

export default function Register() {
  const navigate = useNavigate();
  const [msgApi, contextHolder] = message.useMessage();

  async function onFinish(values: RegisterForm) {
    try {
      await api.post("/v1/auth/register", values);
      msgApi.success("注册成功，请登录");
      navigate("/login", { replace: true });
    } catch (e: any) {
      const detail = e?.response?.data?.detail;
      msgApi.error(detail ? String(detail) : "注册失败");
    }
  }

  return (
    <div style={{ maxWidth: 520, margin: "64px auto", padding: 16 }}>
      {contextHolder}
      <Card>
        <Typography.Title level={3} style={{ marginTop: 0 }}>
          注册
        </Typography.Title>
        <Typography.Paragraph type="secondary">
          仅开发环境允许注册（后端 `ALLOW_REGISTER=1`）。
        </Typography.Paragraph>

        <Form<RegisterForm> layout="vertical" onFinish={onFinish}>
          <Form.Item name="username" label="用户名" rules={[{ required: true }]}>
            <Input autoComplete="username" />
          </Form.Item>
          <Form.Item
            name="password"
            label="密码"
            rules={[
              { required: true },
              { min: 8, message: "至少 8 位" },
            ]}
          >
            <Input.Password autoComplete="new-password" />
          </Form.Item>
          <Form.Item>
            <Button type="primary" htmlType="submit" block>
              注册
            </Button>
          </Form.Item>
        </Form>

        <Typography.Paragraph type="secondary" style={{ marginBottom: 0 }}>
          已有账号？去{" "}
          <a
            href="#"
            onClick={(ev) => {
              ev.preventDefault();
              navigate("/login");
            }}
          >
            登录
          </a>
        </Typography.Paragraph>
      </Card>
    </div>
  );
}

