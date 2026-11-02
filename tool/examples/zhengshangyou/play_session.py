#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
对局会话工具（人类亲自决策版）：
  python play_session.py new             # 新开一局
  python play_session.py view            # 查看当前局面
  python play_session.py play "0,2,4"    # 出牌（手牌编号，逗号分隔）
  python play_session.py pass            # 我不要
  python play_session.py hint            # 列出当前所有可出的合法牌型
状态保存在 session_state.pkl；AI 回合自动执行，每次命令后推进到我的决策点。
"""
import os
import pickle
import sys

if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from game_core import Game, analyze, can_beat, all_plays  # noqa: E402

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'session_state.pkl')
JOKER_CN = {16: '小王', 17: '大王'}
TYPE_CN = {'SINGLE': '单张', 'PAIR': '对子', 'TRIPLE': '三张', 'TRIPLE_ONE': '三带一',
           'TRIPLE_PAIR': '三带二', 'STRAIGHT': '顺子', 'BOMB': '炸弹', 'ROCKET': '王炸'}


def describe(play):
    from game_core import VALUE_NAMES
    t, m, l = play[:3]
    main = JOKER_CN.get(m, VALUE_NAMES.get(m, m))
    if t == 'STRAIGHT':
        return f'{l}张顺子(到{main})'
    if t == 'ROCKET':
        return '王炸'
    return f'{TYPE_CN[t]}(主牌{main})'


def save(g):
    with open(STATE_FILE, 'wb') as f:
        pickle.dump(g, f)


def load():
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE, 'rb') as f:
        return pickle.load(f)


def show(g):
    print('=' * 58)
    print(f'🎴 第 {g.round_no} 轮    牌堆: {g.deck.__len__() if False else len(g.deck)} 张')
    print('🤖 AI 手牌:', '■ ' * len(g.hands[1]), f'({len(g.hands[1])}张)')
    hand = g.hands[0]
    print('😀 你的手牌:', '  '.join(f'[{i}]{c.name()}' for i, c in enumerate(hand)))
    if g.last_play is not None:
        who = '你' if g.last_player == 0 else 'AI'
        print(f'📌 场上待压: {who}出的 {describe(g.last_play)}')
    else:
        print('📌 场上: 自由出牌')
    if g._over:
        print('🏁 游戏结束:', '你赢了！🎉' if g.winner == 0 else 'AI 赢了 😢')
    elif g.turn == 0:
        if g.last_play is not None:
            print('🎯 轮到你: 压牌 —— 输入 play "编号" 或 pass')
        else:
            print('🎯 轮到你: 自由出牌 —— 输入 play "编号"')
    else:
        print('🎯 AI 回合')
    print('────────── 最近动态 ──────────')
    for ev in g.events[-8:]:
        if ev[0] == 'play':
            who = '你' if ev[1] == 0 else 'AI'
            print(f'· {who} 出了 {describe(ev[2])}: {" ".join(c.name() for c in ev[3])}')
        elif ev[0] == 'pass':
            who = '你' if ev[1] == 0 else 'AI'
            print(f'· {who} 不要')
        elif ev[0] == 'draw':
            who = '你' if ev[1] == 0 else 'AI'
            print(f'· 补牌: {who} +{ev[2]}')
        elif ev[0] == 'round_end':
            print(f'· 第{g.round_no - 1}轮结束, {"你" if ev[1] == 0 else "AI"}赢下出牌权')
        elif ev[0] == 'win':
            print(f'· 🏁 {"你" if ev[1] == 0 else "AI"} 率先出完手牌!')
    print('=' * 58)


def advance(g):
    """AI 行动直到轮到玩家或游戏结束"""
    while not g._over and g.turn == 1:
        g.ai_action(1)


def cmd_new():
    g = Game()
    advance(g)
    save(g)
    print(f'🎮 新对局开始！先手: {"你" if g.starter == 0 else "AI"}')
    show(g)


def cmd_view():
    g = load()
    if g is None:
        print('还没有对局，请先执行 new')
        return
    show(g)


def cmd_hint():
    g = load()
    if g is None or g._over or g.turn != 0:
        print('当前不需要你出牌')
        return
    plays = all_plays(g.hands[0])
    if g.last_play is not None:
        plays = [p for p in plays if can_beat(p, g.last_play)]
        if not plays:
            print('没有能压住的牌型，只能 pass')
            return
        print('可压的牌型:')
    else:
        print('可出的牌型:')
    for p in plays:
        print(f'  {describe(p):<14} {" ".join(c.name() for c in p[3])}')


def cmd_play(arg):
    g = load()
    if g is None:
        print('还没有对局，请先执行 new')
        return
    if g._over:
        print('对局已结束，请 new 重开')
        return
    if g.turn != 0:
        print('还没轮到你')
        return
    try:
        idxs = [int(x) for x in arg.replace('，', ',').split(',') if x.strip()]
    except ValueError:
        print('编号无效')
        return
    try:
        cards = [g.hands[0][i] for i in idxs]
    except IndexError:
        print('编号超出范围')
        return
    play = analyze(cards)
    if play is None:
        print('✗ 不是合法牌型（单张/对子/三张/三带一/三带二/顺子2~5张/炸弹/王炸）')
        show(g)
        return
    if g.last_play is not None and not can_beat(play, g.last_play):
        print('✗ 压不过上一手牌')
        show(g)
        return
    play = (play[0], play[1], play[2], cards)
    g.player_play(0, play)
    if not g._over:
        advance(g)
    save(g)
    show(g)


def cmd_pass():
    g = load()
    if g is None:
        print('还没有对局，请先执行 new')
        return
    if g._over:
        print('对局已结束，请 new 重开')
        return
    if g.turn != 0:
        print('还没轮到你')
        return
    if g.last_play is None:
        print('自由出牌轮不能 pass')
        return
    g.player_pass(0)
    if not g._over:
        advance(g)
    save(g)
    show(g)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    c = sys.argv[1]
    if c == 'new':
        cmd_new()
    elif c == 'view':
        cmd_view()
    elif c == 'hint':
        cmd_hint()
    elif c == 'pass':
        cmd_pass()
    elif c == 'play':
        if len(sys.argv) < 3:
            print('用法: play "编号,编号"')
            return
        cmd_play(sys.argv[2])
    else:
        print(__doc__)


if __name__ == '__main__':
    main()
