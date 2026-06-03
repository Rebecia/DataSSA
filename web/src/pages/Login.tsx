import { Button, Card, Form, Input, Typography, message } from "antd";
import { useNavigate } from "react-router-dom";

import { api, setToken } from "../api/client";

type LoginForm = {
  username: string;
  password: string;
};

export default function Login() {
  const navigate = useNavigate();

  const [msgApi, contextHolder] = message.useMessage();

  async function onFinish(values: LoginForm) {
    try {
      const resp = await api.post("/v1/auth/login", values);
      setToken(resp.data.access_token);
      msgApi.success("登录成功");
      navigate("/app", { replace: true });
    } catch (e: any) {
      const detail = e?.response?.data?.detail;
      const status = e?.response?.status;
      if (!status) {
        msgApi.error("请求失败：后端未启动或 /api 代理未生效");
        return;
      }
      msgApi.error(detail ? String(detail) : `登录失败（HTTP ${status}）`);
    }
  }

  return (
    <div style={{ maxWidth: 520, margin: "64px auto", padding: 16 }}>
      {contextHolder}
      <Card>
        <Typography.Title level={3} style={{ marginTop: 0 }}>
          DataSSA 登录
        </Typography.Title>

        <Form<LoginForm> layout="vertical" onFinish={onFinish}>
          <Form.Item name="username" label="用户名" rules={[{ required: true }]}>
            <Input autoComplete="username" />
          </Form.Item>
          <Form.Item name="password" label="密码" rules={[{ required: true }]}>
            <Input.Password autoComplete="current-password" />
          </Form.Item>
          <Form.Item>
            <Button type="primary" htmlType="submit" block>
              登录
            </Button>
          </Form.Item>
        </Form>

        <Typography.Paragraph type="secondary" style={{ marginBottom: 0 }}>
          没有账号？去{" "}
          <a
            href="#"
            onClick={(ev) => {
              ev.preventDefault();
              navigate("/register");
            }}
          >
            注册
          </a>
        </Typography.Paragraph>
      </Card>
    </div>
  );
}
