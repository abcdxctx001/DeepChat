# -*- coding: utf-8 -*-
r"""构建 DeepChat 安装器：收集 payload → 打 payload.zip → PyInstaller 单文件 exe。

用法： python tool/installer/make_installer.py
产物： tool/installer/dist/DeepChat-Setup-v<installer_gui.VERSION>.exe

★ 2026-11-11：路径改成**跟着仓库走**（以前写死 D:\downloads\DeepChat / D:\uploads\...，
  换个目录就打包失败）；settings.json 改为**构建时现生成**，把所有 api_key 清空，
  保证安装包里绝不带真实密钥。
"""
import json
import pathlib
import subprocess
import sys
import zipfile

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ROOT = pathlib.Path(__file__).resolve().parents[2]     # 仓库根目录
OUT = ROOT / 'tool' / 'installer'
DIST = OUT / 'dist'

# ★ 2026-11-11：版本号只从 installer_gui.py 取一处，避免这里忘了改
#   （之前这里写死 2.3，installer_gui 已经升到 2.3.1，产出的文件名还是 v2.3）。
def _read_version():
    try:
        import re
        src = (OUT / 'installer_gui.py').read_text(encoding='utf-8')
        m = re.search(r"^VERSION\s*=\s*['\"]([^'\"]+)['\"]", src, re.M)
        if m:
            return m.group(1)
    except Exception:
        pass
    return '0.0'

VERSION = _read_version()

# 装什么：运行必需
#   ★ 2026-11-12：补上 server/workspace/client.zip —— 服务端 CLIENT_ZIP_PATH 就指向这里，
#     少了它别的机器连进来点「下载本地代理」会 404（用户实测）。代价是安装包 +190MB，
#     但这是"多机可用"的前提，必须带。
PAYLOAD = [
    ('server/app.exe', 'server/app.exe'),
    ('server/run.bat', 'server/run.bat'),
    ('server/control.py', 'server/control.py'),
    ('client/agent.exe', 'client/agent.exe'),
    ('client/agent.py', 'client/agent.py'),
    ('client/control.py', 'client/control.py'),
    ('client/run.bat', 'client/run.bat'),
    ('server/workspace/client.zip', 'server/workspace/client.zip'),
]

DIRS = [
    ('server/static', 'server/static'),      # 图标 / 托盘图，control.py 依赖
    ('client/static', 'client/static'),
    ('client/workspace/GUI', 'client/workspace/GUI'),
]


# ★ 2026-11-12（用户要求）：出厂设置**写死**，只带一家 DeepSeek + flash/pro 两个模型。
#   以前是"复制开发机的 settings.json 再把 api_key 清空"，结果把本机的 Ollama、
#   deepseek1 一起带给了用户。价格取自官方定价页（元 / 百万 tokens，[空闲, 高峰]）。
FACTORY_PROVIDER = {
    'name': 'DeepSeek',
    'base_url': 'https://api.deepseek.com',
    'api_key': '',
    # id 留空：服务端 _norm_provider 会按 name+base_url 生成稳定哈希
    'models': [
        {
            'alias': 'flash', 'label': 'flash', 'name': 'deepseek-v4-flash',
            'ctx': '1M', 'max_out': '384K',
            'input_types': ['text', 'image'],
            'price': {'cached': [0.02, 0.04], 'uncached': [1.0, 2.0], 'output': [4.0, 8.0]},
        },
        {
            'alias': 'pro', 'label': 'pro', 'name': 'deepseek-v4-pro',
            'ctx': '1M', 'max_out': '384K',
            # pro 不支持图像理解
            'input_types': ['text'],
            'price': {'cached': [0.15, 0.30], 'uncached': [4.5, 9.0], 'output': [13.5, 27.0]},
        },
    ],
}


def make_factory_settings(dst):
    data = {
        'flash_model': 'deepseek-v4-flash',
        'pro_model': 'deepseek-v4-pro',
        'base_url': 'https://api.deepseek.com',
        'api_key': '',
        'prices': {
            'flash': {'cached': [0.02, 0.04], 'uncached': [1.0, 2.0], 'output': [4.0, 8.0]},
            'pro': {'cached': [0.15, 0.30], 'uncached': [4.5, 9.0], 'output': [13.5, 27.0]},
        },
        'web_tool_price_per_1k': 5.0,
        # 全部留空 = 用各模式内置默认提示词
        'prompts': {'chat_base': '', 'agent_base': '', 'child_base': '', 'sections': {}},
        'providers': [json.loads(json.dumps(FACTORY_PROVIDER))],
    }
    dst.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    return dst


def add_file(zf, src, arc):
    if not src.exists():
        print('  ⚠️ 缺文件: %s' % src)
        return 0
    # .zip/.exe 自身已是压缩格式，再 DEFLATE 只浪费构建时间
    if src.suffix.lower() in ('.zip', '.gz', '.7z', '.png', '.jpg'):
        zf.write(src, arc, compress_type=zipfile.ZIP_STORED)
    else:
        zf.write(src, arc)
    return src.stat().st_size


def build_payload():
    pz = OUT / 'payload.zip'
    factory = make_factory_settings(OUT / 'factory_settings.json')
    total = 0
    with zipfile.ZipFile(pz, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for rel, arc in PAYLOAD:
            total += add_file(zf, ROOT / rel, arc)
        for rel, arc in DIRS:
            base = ROOT / rel
            if not base.exists():
                print('  ⚠️ 缺目录: %s' % base)
                continue
            for f in base.rglob('*'):
                if f.is_file():
                    total += add_file(zf, f, arc + '/' + str(f.relative_to(base)).replace('\\', '/'))
        total += add_file(zf, factory, 'server/workspace/settings.json')
        # 空的运行时目录（避免首次启动报缺目录）
        for d in ('server/workspace/memories', 'server/workspace/uploads',
                  'client/workspace/memories', 'server/workspace/logs'):
            zf.writestr(d + '/.keep', '')
    print('  ✅ payload.zip %.1f MB（原始 %.1f MB）'
          % (pz.stat().st_size / 1048576, total / 1048576))
    return pz


def main():
    print('  ① 收集 payload …')
    build_payload()

    print('  ② PyInstaller 打包安装器 …')
    exe_name = 'DeepChat-Setup-v%s' % VERSION
    cmd = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile',
           '--windowed', '--name', exe_name,
           '--icon', str(ROOT / 'tool' / 'launcher' / 'icon.ico'),
           '--add-data', '%s;.' % (OUT / 'payload.zip'),
           '--distpath', str(DIST),
           '--workpath', str(ROOT / '.backups' / 'pack_installer' / 'build'),
           '--specpath', str(ROOT / '.backups' / 'pack_installer'),
           str(OUT / 'installer_gui.py')]
    r = subprocess.run(cmd, cwd=str(ROOT))
    if r.returncode != 0:
        print('  ❌ 打包失败，退出码 %d' % r.returncode)
        return 1
    exe = DIST / (exe_name + '.exe')
    if exe.exists():
        print('  ✅ 安装器: %s  %.1f MB' % (exe, exe.stat().st_size / 1048576))
    else:
        print('  ❌ 没找到产物')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
