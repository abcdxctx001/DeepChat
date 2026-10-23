# 控制面板（开发辅助目录）

> **产品入口不在这里。** 控制面板是 DeepChat 自带的功能，直接用
> `server\run.bat` 或 `client\run.bat` 打开即可（打包版双击 `app.exe` / `agent.exe`，
> 不带参数默认就是打开控制面板，加 `--serve` 才是跑服务）。
>
> 本目录只放**开发与打包用的辅助脚本**。

## 控制面板是什么

一个图形化的服务管理面板，替代"双击两个 run.bat、各开一个黑窗口"的用法：

| 打开方式 | 能管理什么 |
|---|---|
| `server\run.bat` / `app.exe --control` | **服务端 + 本机代理**（两个都行） |
| `client\run.bat` / `agent.exe --control` | **只有本机代理** |

> client 端不管服务端是刻意的：两者甚至可能不在同一台机器上
> （server 可部署在远程，agent 必须与浏览器同机，见根目录 `README.md` 的架构总览）。

## control.py 有四份，但它们必须字节一致

| 路径 | 角色 |
|---|---|
| `server\control.py` | 发行版（服务端） |
| `client\control.py` | 发行版（客户端，单独分发 client.zip 时要自足） |
| `tool\launcher\control.py` | **测试用同源副本** —— `test_control.py` 靠 `import control` 直接加载它 |
| `tool\launcher\`（打包时） | `pack_all.bat` 用的是 `server\control.py` / `client\control.py`，不是这份 |

改任何一份都必须同步另外两份，否则测试测的是旧逻辑。
`python tool\sync_uploads.py --check` 会校验三者 MD5 是否一致（不一致直接判失败）。

## 本目录文件

| 文件 | 说明 |
|---|---|
| `control.py` | 控制面板实现（与 `server/control.py` 同源，见上） |
| `make_icon.py` | 由 `server/static/logo.svg` 生成图标（`icon.png`、`icon.ico`、`tray.png`） |
| `render_logo.mjs` | 用 Chrome 把 SVG 光栅化（本项目没有 SVG 渲染库） |
| `_svg_1024.png` | 上一步的中间产物 |
| `test_control.py` | 无界面验证服务管理逻辑（用测试端口，不碰 5000/5001） |
| `logs/` | 测试用日志目录（运行时数据，不入库、不分发） |

> `test_control.py` 与 `logs/` 属于开发脚手架，**不会进交付目录**
> （`tool/sync_uploads.py` 已排除）。

## 图标是怎么来的

`server/static/logo.svg` 是这个项目**已有的品牌图标**（前端 `index.html` 也拿它当
favicon）。它是矢量图，而本项目没有 SVG 渲染库，所以：

1. `render_logo.mjs` 用 Chrome 把 SVG 渲染成 `_svg_1024.png`
2. `make_icon.py` 给它加圆角渐变底（`#2563eb → #7c3aed`，与前端 logo 同色），
   鲸鱼居中留白，输出 `icon.png` / `icon.ico` / `tray.png`

> 单独一条蓝色鲸鱼直接当应用图标并不好：任务栏底色不可控，深色背景下会糊成一团，
> 所以做成「圆角方块 + 居中白鲸」的标准应用图标样式。

生成结果会被复制到 `server/static/` 与 `client/static/`，
`icon.ico` 同时被 `tool\pack_all.bat` 用作 `app.exe` / `agent.exe` 的图标。

重新生成（在**本目录**下执行）：

```bat
node render_logo.mjs          :: 需要 Chrome 开在 9222 端口（--remote-debugging-port=9222）
python make_icon.py
```

## 跑测试

```bat
python tool\launcher\test_control.py
```

覆盖：启动 / 重复启动 / 停止 / 重复停止 / 端口释放 / 日志编码与 ANSI 净化 / 性能。
