# 步骤 12：本周总验收 + 收尾

## 本步目标

确认第 1 周全部完成；写学习笔记；记录实施结果。

## 前置条件

- 步骤 1–11 均已完成

## 你要做什么

### 1. 跑总验收清单

```text
[ ] docker compose：mysql、redis、api 正常
[ ] alembic：users 表存在
[ ] 后端：注册 / 登录 / me / 无 token→401
[ ] 前端：登录 → /app 显示用户 → 清 token 跳回登录
[ ] 回归：DATASSA_TEST_MODE=1 时 POST /chat 仍可用
```

### 2. 填写学习笔记

手写 [docs/learning/week1.md](../learning/week1.md) 五条要点。

### 3. 填写实施记录

更新 [docs/week1/DONE.md](./DONE.md)（若无则创建）。

### 4. 精读（出声讲一遍）

1. `server/deps.py`
2. `server/api/v1/auth.py`
3. `server/config.py`

## 本步完成标志

- [ ] 总验收全部打勾
- [ ] `week1.md` 已手写
- [ ] `DONE.md` 已填

## 第 2 周入口

见 [ROADMAP_6W.md](../ROADMAP_6W.md) 第 2 周（届时会有 `docs/week2/`，或先用总计划）。

## 给 AI

```text
帮我核对 docs/week1/step-12-final.md 总验收，缺什么补什么（仅限第1周范围）。
```
