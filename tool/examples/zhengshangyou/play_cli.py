#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
改版争上游（2人版）· 命令行版
运行：python play_cli.py
规则与 HTML 版一致；AI 随机出所有合法牌型中的任意一种。
操作：输入手牌编号（逗号分隔）出牌；压牌轮输入 p 表示不要；q 退出。
"""
import sys
from game_core import Game, analyze, can_beat, random_ai

# Windows 控制台默认 GBK，无法编码扑克花色符号，统一改用 UTF-8 输出
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

TYPE_CN = {
    'SINGLE': '单张', 'PAIR': '对子', 'TRIPLE': '三张', 'TRIPLE_ONE': '三带一',
    'TRIPLE_PAIR': '三带二', 'STRAIGHT': '顺子', 'BOMB': '炸弹', 'ROCKET': '王炸',
}
JOKER_CN = {16: '小王', 17: '大王'}


def describe_play(play):
    from game_core import VALUE_NAMES
    t, m, l = play[:3]
    if t == 'STRAIGHT':
        return f'{l}张顺子（到 {JOKER_CN.get(m, VALUE_NAMES.get(m, m))}）'
    if t == 'ROCKET':
        return '王炸'
    return f'{TYPE_CN[t]}（主牌 {JOKER_CN.get(m, VALUE_NAMES.get(m, m))}）'


def main():
    g = Game()
    print('=' * 56)
    print('  改版争上游 2人版（命令行）· 手牌上限5张 · 顺子2张起')
    print('  先手：' + ('你' if g.starter == 0 else 'AI') + '  牌堆：%d 张' % len(g.deck))
    print('=' * 56)
    last_draw_pos = 0

    while not g._over:
        # 展示补牌信息（事件流中新增的 draw）
        for ev in g.events[last_draw_pos:]:
            if ev[0] == 'draw':
                who = '你' if ev[1] == 0 else 'AI'
                if ev[2] > 0:
                    print(f'  [补牌] {who} 补了 {ev[2]} 张')
            elif ev[0] == 'round_end':
                print(f'  [轮次] 第{g.round_no - 1}轮结束，{"你" if ev[1] == 0 else "AI"}赢下出牌权')
        last_draw_pos = len(g.events)

        print(f'\n───── 第 {g.round_no} 轮 ─────  牌堆剩余: {len(g.deck)} 张')
        print('你的手牌:', '  '.join(f'{i}:{c.name()}' for i, c in enumerate(g.hands[0])))
        print('AI 手牌 :', ' '.join('■' for _ in g.hands[1]), f'({len(g.hands[1])}张)')
        if g.last_play is not None:
            print(f'场上待压: {"你" if g.last_player == 0 else "AI"} 出的 {describe_play(g.last_play)}')
        else:
            print('场上: 自由出牌')

        if g.turn == 1:
            play = random_ai(g.hands[1], g.last_play, g.rng)
            if play is None:
                print('AI: 不要')
                g.player_pass(1)
            else:
                print('AI 出了:', ' '.join(c.name() for c in play[3]), f'（{describe_play(play)}）')
                g.player_play(1, play)
        else:
            plays = g.current_legal_plays(0)
            if g.last_play is None:
                prompt = '  请出牌（输入编号，逗号分隔；q 退出）\n> '
            else:
                prompt = '  请出牌（输入编号，逗号分隔；p 不要；q 退出）\n> '
            while True:
                try:
                    cmd = input(prompt).strip().lower()
                except (EOFError, KeyboardInterrupt):
                    print('\n再见！')
                    return
                if cmd == 'q':
                    return
                if cmd == 'p' and g.last_play is not None:
                    g.player_pass(0)
                    print('  你不要')
                    break
                try:
                    idxs = [int(x) for x in cmd.replace('，', ',').split(',') if x.strip()]
                    if not idxs:
                        print('  输入无效')
                        continue
                    cards = [g.hands[0][i] for i in idxs]
                except (ValueError, IndexError):
                    print('  输入无效，请重新输入')
                    continue
                play = analyze(cards)
                if play is None:
                    print('  不是合法牌型（单张/对子/三张/三带一/三带二/顺子2~5张/炸弹/王炸）')
                    continue
                if g.last_play is not None and not can_beat(play, g.last_play):
                    print('  压不过上一手牌')
                    continue
                play = (play[0], play[1], play[2], cards)
                g.player_play(0, play)
                print('  你出了:', ' '.join(c.name() for c in cards), f'（{describe_play(play)}）')
                break

    print('\n' + '=' * 56)
    if g.winner == 0:
        print('  🏆 你赢了！率先出完手牌！')
    else:
        print('  🤖 AI 赢了，再接再厉！')
    print('=' * 56)


def auto_run():
    """AI vs AI 自动对弈（--auto 模式，用于测试与演示完整流程）"""
    g = Game()
    while not g._over:
        g.ai_action(g.turn)
    print('=' * 56)
    print('自动对局结束：' + ('你赢了' if g.winner == 0 else 'AI赢了') +
          f'，共 {g.round_no} 轮，胜者出完手牌')
    print('=' * 56)


if __name__ == '__main__':
    if '--auto' in sys.argv:
        auto_run()
    else:
        main()
