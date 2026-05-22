# 步骤 9：初始化 React 工程

## 本步目标

`web/` 目录可 `npm run dev`；能打开空白页。

## 前置条件

- [步骤 8](./step-08-verify-api.md) **已通过**

## 你要做什么

1. 创建 Vite + React + TypeScript：`web/`
2. 安装：`react-router-dom`、`axios`、`antd`
3. 配置 `vite.config.ts`：dev 代理 `/api` → `http://127.0.0.1:18790`
4. `App.tsx` 先放最简路由骨架（/login、/app）

## 涉及文件

| 操作 | 路径 |
|------|------|
| 新建 | `web/**`（Vite 标准结构） |

## 本步完成标志

```bash
cd web && npm install && npm run dev
```

- [ ] 浏览器打开 `http://localhost:5173` 有页面
- [ ] **尚未要求** 登录功能（步骤 10）

## 下一步

[步骤 10：登录页](./step-10-react-login.md)
