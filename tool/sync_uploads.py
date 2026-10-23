#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""把仓库同步到三个交付目录（D:\uploads），可重复执行。

════════════════════════════════════════════════════════════════════════
  三个交付目录
════════════════════════════════════════════════════════════════════════
  1. D:\uploads\DeepChat                源码版（推 GitHub）
       · 不含 exe / client.zip
       · 不含任何 settings.json（密钥绝不入库、也不该出现在源码包）

  2. D:\uploads\DeepChat-出厂设置版      发给别人用的成品
       · 含 app.exe / agent.exe / client.zip
       · settings.json = **出厂配置**：
           - providers 只有 DeepSeek 一家，含 flash + pro 两个模型
           - api_key 为空（用户自己填）
           - 价格用官方默认
       · 绝不写真实 key

  3. D:\uploads\DeepChat-可运行版        自己用的
       · 含 exe / client.zip
       · settings.json = 直接复制开发机的真实配置（真 key + flash/Qwen）

════════════════════════════════════════════════════════════════════════
  用法
════════════════════════════════════════════════════════════════════════
    python tool/sync_uploads.py                  # 三个都同步
    python tool/sync_uploads.py --only runnable  # 只同步可运行版
    python tool/sync_uploads.py --only factory,source
    python tool/sync_uploads.py --dry-run        # 只报告，不写文件
    python tool/sync_uploads.py --check          # 只做安全/完整性校验

════════════════════════════════════════════════════════════════════════
  排除规则
════════════════════════════════════════════════════════════════════════
  永远不复制：git 元数据、备份、测试脚手架、__pycache__、
              运行时数据（对话树 / 记忆 / 上传 / 工具输出 / 日志 / PID / 会话统计）
  按目录区分：exe 与 client.zip 只进"出厂设置版 / 可运行版"
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

REPO = Path(__file__).resolve().parent.parent
UPLOADS = Path(r'D:\uploads')

# ── 目录名 ──
DIR_SOURCE = 'DeepChat'
DIR_FACTORY = 'DeepChat-出厂设置版'
DIR_RUNNABLE = 'DeepChat-可运行版'

# ── 永远不复制（路径片段，正斜杠匹配）──
EXCLUDE_ANY = {
    '.git', '.backups', '.test', '__pycache__', '.pytest_cache',
    'node_modules', '.idea', '.vscode',
}
# 运行时数据（按相对路径前缀排除）
EXCLUDE_PREFIX = (
    'server/logs/',
    'server/workspace/memories/',
    'server/workspace/uploads/',
    'server/workspace/session_stats/',
    'server/workspace/tool_outputs/',
    'client/workspace/memories/',
    'client/workspace/screenshots/',
    'client/workspace/tool_outputs/',
    'client/workspace/tool_test/',
    'client/workspace/tool_test_demo/',
    'client/workspace/test_sandbox/',
    'client/workspace/tests/',
    'client/workspace/node/',
    'client/workspace/python/',
    'client/workspace/mingw64/',
    'client/workspace/capture/',
    # ★ 模型跑 C++ 测试时留下的编译产物（调试版 exe 一堆，不属于交付物）
    'client/workspace/boat_dbg/',
    # ★ 第三方工具包与数据（.gitignore 已排除，也不该进交付目录）
    'tool/data/',
    'tool/SNIBypassGUI-v4.6/',
    'tool/logs/',
    'tool/launcher/logs/',
    'tool/launcher/__pycache__/',
    # ★ 模型/我跑测试时留下的嵌套 workspace（test_env_check.txt、tool_test/ 等）
    'client/workspace/workspace/',
    # ★ 其它测试目录 / 临时产物
    'client/workspace/retest/',
    'client/workspace/scratch/',
    'server/logs/thinking_dump/',
    # ★ 2026-10-21：文档合并为**单一根 README**，交付目录里不再保留 server/README.md
    'server/README.md',
)
# 运行时文件（按文件名/后缀排除）
EXCLUDE_NAME_SUFFIX = ('.pyc', '.pyo', '.log', '.pid', '.bak', '.pricebak',
                       '.deepseek-bak', '.before_restore')
EXCLUDE_NAME_EXACT = {
    'chat_tree.json', 'chat_tree.bak',
    'settings.json', 'settings.json.deepseek-bak', 'settings.json.pricebak',
    'panel_ready',
    # ★ 测试脚手架 / 我自用的临时脚本（按用户要求：测试文件不留）
    '_test_agent.py', '_test_server.py',
    '_integrate_to_czz.py', '_start_final.py',
    'test_headless.py', 'test_control.py',
    '重启测试服务.bat', '小模型.txt',
    'restart_server.py',
    # ★ 自嵌套冗余（用户已明确要求不再分发）
    'server.zip',
}

# ★ 「可运行版」要带的**运行时数据**（自用版的对话树 + 记忆库）
#   源码版 / 出厂版一律不带（出厂版是发给别人的，绝不能带我的数据）
RUNTIME_DATA_PREFIXES = (
    'server/workspace/memories/',
    'client/workspace/memories/',
)
RUNTIME_DATA_FILES = {
    'server/workspace/chat_tree.json',
}

# ★ client/workspace 顶层只保留**功能文件**
#   （fetch_page.js 被 agent.py/app.py 引用；其余是跑测试留下的脚本/报告，按用户要求不留）
WS_WHITELIST = {'fetch_page.js'}

# 二进制/大件（只进成品目录）
BIG_FILES = {
    'server/app.exe',
    'client/agent.exe',
    'server/workspace/client.zip',
    'server/workspace/server.zip',      # 已废弃，不再分发
}

# ── 出厂设置：DeepSeek 两个模型，key 为空 ──
FACTORY_SETTINGS = {
    "base_url": "https://api.deepseek.com",
    "api_key": "",
    "web_tool_price_per_1k": 5.0,
    "prompts": {"chat_base": "", "agent_base": "", "child_base": "", "sections": {}},
    "providers": [
        {
            "name": "DeepSeek",
            "base_url": "https://api.deepseek.com",
            "api_key": "",
            "enabled": True,
            "models": [
                {
                    "alias": "flash", "label": "flash", "name": "deepseek-v4-flash",
                    "ctx": "", "max_out": "", "input_types": ["text", "image"],
                    "price": {"cached": [0.02, 0.04], "uncached": [1.0, 2.0],
                              "output": [4.0, 8.0]},
                },
                {
                    "alias": "pro", "label": "pro", "name": "deepseek-v4-pro",
                    "ctx": "", "max_out": "", "input_types": ["text", "image"],
                    "price": {"cached": [0.15, 0.30], "uncached": [4.5, 9.0],
                              "output": [13.5, 27.0]},
                },
            ],
        },
    ],
}


def md5(p: Path) -> str:
    h = hashlib.md5()
    with open(p, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def rel_str(p: Path) -> str:
    return p.relative_to(REPO).as_posix()


# ★ workspace 白名单：**默认拒绝**，只放行明确需要的条目
#   （仓库 client/workspace 下有 30 个测试目录 + 30 个测试文件，
#     用黑名单永远列不全 —— 实测被用户抓出 agent_demo/agent_test/
#     consistency_retest/rating_check/rating_work/retest_readfile/
#     subagent_model_test/tooltest/truncation_test 等一大批残留）
WS_CLIENT_DIRS = {'GUI'}          # PIL，图片类工具需要
WS_CLIENT_FILES = {'fetch_page.js'}
# server/workspace 只放行这两个文件（其余全拒），memories 由 with_data 决定


def should_skip(rel: str, with_exe: bool = False, with_data: bool = False) -> bool:
    """该文件是否**不**交付。"""
    parts = rel.split('/')
    # ① 版本库/备份/缓存，一律不要
    if any(x in EXCLUDE_ANY for x in parts):
        return True

    # ② 大件：没有 exe 的交付（源码版）不要
    if rel in BIG_FILES and not with_exe:
        return True
    if rel.endswith('.zip') and not with_exe:
        return True
    if rel.endswith('.zip') and not rel.endswith('client.zip'):
        return True          # server.zip 等其余压缩包都不分发

    # ③ client/workspace：白名单
    if rel.startswith('client/workspace/'):
        rest = rel[len('client/workspace/'):]
        top = rest.split('/')[0]
        if '/' in rest:                       # 子目录里的文件
            if top in WS_CLIENT_DIRS:
                return False
            if with_data and top == 'memories':
                return False
            return True
        return rest not in WS_CLIENT_FILES   # 顶层文件

    # ④ server/workspace：白名单
    if rel.startswith('server/workspace/'):
        rest = rel[len('server/workspace/'):]
        top = rest.split('/')[0]
        if rel in BIG_FILES:                  # client.zip
            return False
        if with_data and rest == 'chat_tree.json':
            return False
        if with_data and ('/' in rest) and top == 'memories':
            return False
        return True                           # settings.json 另写；uploads/ 等一律不要

    # ⑤ 其它目录：沿用通用排除
    if any(rel.startswith(pre) for pre in EXCLUDE_PREFIX):
        return True
    name = parts[-1]
    if name in EXCLUDE_NAME_EXACT:
        return True
    if any(name.endswith(sfx) for sfx in EXCLUDE_NAME_SUFFIX):
        return True
    return False


def iter_sources(with_exe: bool, with_data: bool = False):
    """遍历仓库里应交付的文件。"""
    for p in sorted(REPO.rglob('*')):
        if not p.is_file():
            continue
        rel = rel_str(p)
        if should_skip(rel, with_exe, with_data):
            continue
        if not with_exe and rel in BIG_FILES:
            continue
        if rel.endswith('.zip') and not with_exe:
            continue
        yield p, rel


def copy_tree(dst_root: Path, with_exe: bool, dry: bool, report: list,
              with_data: bool = False):
    """把仓库文件镜像到 dst_root；只覆盖有变化的文件。"""
    n_new = n_upd = n_same = 0
    for src, rel in iter_sources(with_exe, with_data):
        dst = dst_root / rel
        try:
            if dst.exists() and dst.stat().st_size == src.stat().st_size and md5(dst) == md5(src):
                n_same += 1
                continue
            is_new = not dst.exists()
            if not dry:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            if is_new:
                n_new += 1
            else:
                n_upd += 1
                report.append(('更新', rel))
        except Exception as e:
            report.append(('失败', f'{rel}: {e}'))
    return n_new, n_upd, n_same


# 这些是"合法产物"：本来就不从仓库复制，但必须留在交付目录里
PRUNE_KEEP = {
    'server/workspace/settings.json',
    'server/workspace/client.zip',
    'server/app.exe',
    'client/agent.exe',
}


def prune(dst_root: Path, dry: bool, report: list, with_data: bool = False) -> int:
    """删除目标目录里**命中排除规则**的文件/空目录（copy_tree 只增不删，必须补这一步）。

    为什么需要：排除规则只能阻止"复制新的"，已经存在的旧文件会一直留着 ——
    实测可运行版里曾残留 chat_tree.json（10 MB 真实对话记录）、记忆库、
    logs/、__pycache__/、retest/ 等，用户明确要求"测试文件不用留"。
    """
    if not dst_root.exists():
        return 0
    removed = 0
    # 先删文件
    for f in sorted(dst_root.rglob('*'), key=lambda x: -len(x.parts)):
        if not f.is_file():
            continue
        rel = f.relative_to(dst_root).as_posix()
        if rel in PRUNE_KEEP:
            continue
        if not should_skip(rel, True, with_data):
            continue
        # 大件在成品目录里是合法的（虽然不在仓库，也不该被删）
        if rel in BIG_FILES:
            continue
        if not dry:
            try:
                f.unlink()
            except Exception as e:
                report.append(('删除失败', f'{rel}: {e}'))
                continue
        report.append(('删除', rel))
        removed += 1
    # 再删空目录（自底向上）
    for d in sorted((x for x in dst_root.rglob('*') if x.is_dir()),
                    key=lambda x: -len(x.parts)):
        try:
            if not any(d.iterdir()):
                if not dry:
                    d.rmdir()
                removed += 1
        except Exception:
            pass
    return removed


def write_settings(dst_root: Path, content: dict, dry: bool, label: str):
    p = dst_root / 'server' / 'workspace' / 'settings.json'
    if dry:
        print(f'    [dry] 将写入 {label} -> {p}')
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'    写入 {label}: {p}')


def sync_source(dry: bool, report: list):
    root = UPLOADS / DIR_SOURCE
    print(f'  [{DIR_SOURCE}] 源码版（不含 exe / 不含 settings.json）')
    n_new, n_upd, n_same = copy_tree(root, with_exe=False, dry=dry, report=report)
    n_del = prune(root, dry, report)
    print(f'    清理 {n_del} 项不该交付的文件')
    # 源码版不应有 settings.json / exe
    if not dry:
        for junk in ('server/workspace/settings.json', 'server/app.exe', 'client/agent.exe'):
            f = root / junk
            if f.exists():
                f.unlink()
                report.append(('删除', f'{junk}（源码版不该有）'))
    print(f'    新增 {n_new} / 更新 {n_upd} / 未变 {n_same}')


def sync_factory(dry: bool, report: list):
    root = UPLOADS / DIR_FACTORY
    print(f'  [{DIR_FACTORY}] 出厂设置版（含 exe，DeepSeek 两模型，key 为空）')
    n_new, n_upd, n_same = copy_tree(root, with_exe=True, dry=dry, report=report)
    n_del = prune(root, dry, report)
    print(f'    新增 {n_new} / 更新 {n_upd} / 未变 {n_same} / 清理 {n_del}')
    write_settings(root, FACTORY_SETTINGS, dry, '出厂配置（无 key）')


def sync_runnable(dry: bool, report: list):
    root = UPLOADS / DIR_RUNNABLE
    print(f'  [{DIR_RUNNABLE}] 可运行版（含 exe，真实 key + flash/Qwen）')
    n_new, n_upd, n_same = copy_tree(root, with_exe=True, dry=dry, report=report,
                                     with_data=True)
    n_del = prune(root, dry, report, with_data=True)
    print(f'    新增 {n_new} / 更新 {n_upd} / 未变 {n_same} / 清理 {n_del}'
          f'（含运行时数据：对话树 + 记忆库）')
    dev = REPO / 'server' / 'workspace' / 'settings.json'
    if not dev.exists():
        print('    ⚠️ 开发机没有 settings.json —— 跳过（否则会清掉你的真实配置）')
        return
    real = json.loads(dev.read_text(encoding='utf-8'))
    write_settings(root, real, dry, '真实配置（含真 key）')


# ══════════════════════════════════════════════════════════════════
#  校验
# ══════════════════════════════════════════════════════════════════
def _scan_keys(root: Path):
    """返回 (真实key列表, 梗占位列表)。"""
    import re
    real, joke = [], []
    PAT = re.compile(r'sk-[A-Za-z0-9]{20,}')
    for p in root.rglob('*'):
        if not p.is_file() or p.stat().st_size > 5 * 1024 * 1024:
            continue
        if p.suffix.lower() not in ('.json', '.py', '.md', '.bat', '.txt', '.js',
                                    '.html', '.css', '.ps1', ''):
            continue
        try:
            t = p.read_text(encoding='utf-8', errors='replace')
        except Exception:
            continue
        for m in PAT.findall(t):
            (joke if ('KFC' in m or 'Todayis' in m) else real).append((p, m[:8] + '…'))
    return real, joke


def check(dry: bool = False):
    print()
    print('=' * 70)
    print('  交付校验')
    print('=' * 70)
    ok = True

    # 1) 源码版绝不能有真实 key / exe
    src = UPLOADS / DIR_SOURCE
    if src.exists():
        real, _ = _scan_keys(src)
        if real:
            ok = False
            print(f'  ❌ 源码版含真实 key {len(real)} 处:')
            for p, k in real[:6]:
                print(f'       {p}  {k}')
        else:
            print('  ✅ 源码版无真实 key')
        has_exe = list(src.rglob('*.exe'))
        print(f'  {"❌ 源码版含 exe" if has_exe else "✅ 源码版无 exe"}'
              + (f' {[p.name for p in has_exe[:3]]}' if has_exe else ''))
        if has_exe:
            ok = False

    # 2) 出厂设置版：无真实 key、有 exe、settings 含 DeepSeek 两模型
    fac = UPLOADS / DIR_FACTORY
    if fac.exists():
        real, _ = _scan_keys(fac)
        if real:
            ok = False
            print(f'  ❌ 出厂设置版含真实 key {len(real)} 处')
            for p, k in real[:6]:
                print(f'       {p}  {k}')
        else:
            print('  ✅ 出厂设置版无真实 key')
        for need in ('server/app.exe', 'client/agent.exe', 'server/workspace/client.zip'):
            print(f'  {"✅" if (fac / need).exists() else "❌ 缺"} {need}')
        st = fac / 'server' / 'workspace' / 'settings.json'
        if st.exists():
            d = json.loads(st.read_text(encoding='utf-8'))
            provs = d.get('providers') or []
            k = str(d.get('api_key') or '')
            models = [m.get('alias') or m.get('name') for p2 in provs
                      for m in (p2.get('models') or [])]
            good = (not k) and len(provs) == 1 and provs[0].get('name') == 'DeepSeek' \
                and set(models) >= {'flash', 'pro'}
            print(f'  {"✅" if good else "❌"} 出厂 settings: '
                  f'key={"空" if not k else "非空!"} providers={len(provs)} models={models}')
            if not good:
                ok = False
        else:
            ok = False
            print('  ❌ 出厂设置版缺 settings.json')

    # 3) 可运行版：有真实 key、有 exe、模型含 flash / Qwen
    run = UPLOADS / DIR_RUNNABLE
    if run.exists():
        st = run / 'server' / 'workspace' / 'settings.json'
        if st.exists():
            d = json.loads(st.read_text(encoding='utf-8'))
            provs = d.get('providers') or []
            k = str(d.get('api_key') or '')
            models = [m.get('alias') or m.get('name') for p2 in provs
                      for m in (p2.get('models') or [])]
            has_key = bool(k) and '*' not in k
            print(f'  {"✅" if has_key else "❌ 缺"} 可运行版真实 key'
                  f'（{k[:6]}…{k[-4:]}）' if has_key else '  ❌ 可运行版缺真实 key')
            print(f'  可运行版 providers={len(provs)} models={models}')
            if not has_key:
                ok = False
        else:
            ok = False
            print('  ❌ 可运行版缺 settings.json')
        for need in ('server/app.exe', 'client/agent.exe', 'server/workspace/client.zip'):
            print(f'  {"✅" if (run / need).exists() else "❌ 缺"} {need}')

    # 4) control.py 三份必须完全一致（launcher 是测试用的同源副本）
    cps = ['server/control.py', 'client/control.py', 'tool/launcher/control.py']
    hs = {}
    for rel in cps:
        q = REPO / rel
        if q.exists():
            hs[rel] = md5(q)
    if len(set(hs.values())) == 1 and len(hs) == 3:
        print(f'  ✅ control.py 三份一致（{list(hs.values())[0][:10]}）')
    else:
        ok = False
        print('  ❌ control.py 不一致（改完要同步：server → client → tool/launcher）')
        for rel, h in hs.items():
            print(f'       {h[:10]}  {rel}')

    print()
    print('  ' + ('✅ 全部校验通过' if ok else '❌ 校验未通过（见上）'))
    return ok


def summarize(report: list):
    if not report:
        return
    print()
    print('=' * 70)
    print('  变更明细')
    print('=' * 70)
    for kind, item in report[:60]:
        print(f'  [{kind}] {item}')
    if len(report) > 60:
        print(f'  … 共 {len(report)} 条')


def main():
    ap = argparse.ArgumentParser(description='同步仓库到 uploads 三个交付目录')
    ap.add_argument('--only', default='source,factory,runnable',
                    help='逗号分隔：source / factory / runnable')
    ap.add_argument('--dry-run', action='store_true', help='只报告不改文件')
    ap.add_argument('--check', action='store_true', help='只做校验')
    args = ap.parse_args()

    if args.check:
        sys.exit(0 if check() else 1)

    only = {x.strip() for x in args.only.split(',') if x.strip()}
    print(f'  仓库 : {REPO}')
    print(f'  目标 : {UPLOADS}')
    print(f'  模式 : {"DRY-RUN" if args.dry_run else "实际写入"}  目录: {sorted(only)}')
    print()

    if not REPO.joinpath('server', 'app.py').exists():
        print('  ❌ 这不像 DeepChat 仓库（缺 server/app.py）')
        sys.exit(1)

    report = []
    if 'source' in only:
        sync_source(args.dry_run, report)
    if 'factory' in only:
        sync_factory(args.dry_run, report)
    if 'runnable' in only:
        sync_runnable(args.dry_run, report)

    summarize(report)
    if not args.dry_run:
        sys.exit(0 if check() else 1)


if __name__ == '__main__':
    main()
