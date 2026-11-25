# penpot-auth

自托管 [Penpot](https://penpot.app) 的“用户名登录 / 自助注册”旁路服务（sidecar）。

Penpot 原生只支持邮箱登录。这个小服务用 Python 标准库实现，挂在 Caddy 前面接管 `/auth/*`：

- **注册**：把用户名做 NFKC + casefold 规范化后取 SHA-256，生成一个固定的内部邮箱
  `<hash>@users.pen.moonl.cloud`，通过 Penpot 后端的 PREPL 端口（`127.0.0.1:6063`）调用 `create-profile` 创建账号；
- **登录**：用同样的映射调用 Penpot RPC `login-with-password`（`127.0.0.1:6060`），把返回的会话 Cookie 原样转给浏览器；
- **跳转桥**：代理 `/js/config.js` 并在末尾追加 `bridge.js`，把 Penpot 前端的 `auth-login` / `auth-register` 页面重定向到本服务的表单（邀请链接除外）；
- 自带简单限流（登录 30 次 / 5 分钟，注册 8 次 / 小时，按 Caddy 传入的客户端地址）、同源校验、严格 CSP，表单页为中文。

| 文件 | 作用 |
| --- | --- |
| `auth.py` | 服务本体，监听 Unix 套接字 `/run/penpot-auth/http.sock` |
| `account.html` / `account.css` | 登录 / 注册表单模板与样式 |
| `bridge.js` | 注入 Penpot 前端配置的跳转脚本 |
| `penpot-auth.caddy` | Caddy 站点片段（`/auth/*`、`/js/config.js` 走本服务，禁止修改邮箱，其余反代到 Penpot） |
| `penpot-auth.service` | systemd 单元：用 podman 以只读、无特权的 Python 容器运行 |

## 本地运行

只依赖 Python 3.12+ 标准库，但需要一个可访问的 Penpot 后端（RPC `6060`、PREPL `6063`）和前端（`penpot-frontend:8080`）：

```sh
sudo install -d -m 0755 -o "$USER" /run/penpot-auth
python3 -I auth.py      # 然后用 curl --unix-socket /run/penpot-auth/http.sock http://x/auth/login 访问
```

检查与 CI 一致：

```sh
ruff check .
```

## 在 moon 上的部署

- 代码放在 `/opt/penpot-auth`（只读挂载进容器的 `/app`）；
- `penpot-auth.service` 用 `podman run` 启动固定摘要的 `python` 镜像，加入 `penpot_penpot-backend_1` 容器的网络命名空间，
  只读根文件系统、`--cap-drop ALL`、128 MB 内存上限，套接字目录 `/run/penpot-auth/socket` 由 Caddy 组读取；
- `penpot-auth.caddy` 被 `pen.moonl.cloud` 站点块导入。

这些文件里写死了 moon 的环境：域名 `pen.moonl.cloud`、管理员账号 `owner@pen.moonl.cloud`、容器名、UID/GID（`1001:988`）
和 Penpot 前端反代端口 `19610`。在别处部署时需要按实际环境修改。

## 许可证

[MIT](LICENSE)。Penpot 本身是 MPL-2.0 许可的独立项目，本仓库不包含其代码。
