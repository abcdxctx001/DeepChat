#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
改版争上游（2人版）核心逻辑
====================================
规则：
1. 1副54张扑克，2人对战；每人手牌上限5张，开局各抽5张。
2. 牌型与普通争上游一致，但顺子由5张起步改为2张起步（受手牌上限5张限制，
   有效牌型为：单张/对子/三张/三带一/三带二/顺子(2~5张)/炸弹/王炸）。
3. 依次出牌；若某人出牌后其他人都不要，则从“第一个不要的人”开始，
   所有人依次补齐5张手牌；牌堆无牌则停止补牌。
4. 手牌率先出完者胜，无论牌堆是否还有剩余牌。

点数大小：3 < 4 < ... < 10 < J < Q < K < A < 2 < 小王 < 大王
顺子：3~A 之间连续（不含2与王），长度2~5张，必须等长才能压。
牌型表示：play = (type, main, length, cards)
  type: SINGLE/PAIR/TRIPLE/TRIPLE_ONE/TRIPLE_PAIR/STRAIGHT/BOMB/ROCKET
"""
import random
from collections import Counter

VALUE_NAMES = {3:'3',4:'4',5:'5',6:'6',7:'7',8:'8',9:'9',10:'10',
               11:'J',12:'Q',13:'K',14:'A',15:'2'}
SUIT_SYMBOLS = {'S':'♠','H':'♥','C':'♣','D':'♦'}


class Card:
    __slots__ = ('value', 'suit')
    def __init__(self, value, suit):
        self.value = value          # 3..15 普通牌, 16=小王, 17=大王
        self.suit = suit            # 'S','H','C','D' 或 None(王)

    def name(self):
        if self.value == 16: return '小王'
        if self.value == 17: return '大王'
        return SUIT_SYMBOLS[self.suit] + VALUE_NAMES[self.value]

    def is_joker(self):
        return self.value in (16, 17)

    def __repr__(self):
        return self.name()

    def __eq__(self, other):
        return isinstance(other, Card) and self.value == other.value and self.suit == other.suit

    def __hash__(self):
        return hash((self.value, self.suit))


def build_deck():
    deck = []
    for value in range(3, 16):
        for suit in ('S', 'H', 'C', 'D'):
            deck.append(Card(value, suit))
    deck.append(Card(16, None))   # 小王
    deck.append(Card(17, None))   # 大王
    return deck


def _is_straight_values(values):
    """顺子判定：2~5张，值都在3~14（不含2与王），连续且不重复"""
    if len(values) < 2 or len(values) > 5:
        return False
    if any(v > 14 or v < 3 for v in values):
        return False
    if len(set(values)) != len(values):
        return False
    return max(values) - min(values) == len(values) - 1


def analyze(cards):
    """
    分析一手牌，返回 (type, main, length) 或 None（非法牌型）。
    main 为主比较点数：单张/对子/三张/三带一/三带二/炸弹为该点数，顺子为最大牌。
    """
    n = len(cards)
    if n < 1 or n > 5:
        return None
    values = sorted(c.value for c in cards)
    counts = Counter(values)

    if n == 1:
        return ('SINGLE', values[0], 1)
    if n == 2:
        if set(values) == {16, 17}:
            return ('ROCKET', 17, 2)
        if values[0] == values[1]:
            return ('PAIR', values[0], 2)
        if _is_straight_values(values):
            return ('STRAIGHT', values[-1], 2)
        return None
    if n == 3:
        if len(counts) == 1:
            return ('TRIPLE', values[0], 3)
        if _is_straight_values(values):
            return ('STRAIGHT', values[-1], 3)
        return None
    if n == 4:
        if len(counts) == 1:
            return ('BOMB', values[0], 4)
        if sorted(counts.values()) == [1, 3]:
            main = next(v for v, c in counts.items() if c == 3)
            return ('TRIPLE_ONE', main, 4)
        if _is_straight_values(values):
            return ('STRAIGHT', values[-1], 4)
        return None
    # n == 5
    if sorted(counts.values()) == [2, 3]:
        main = next(v for v, c in counts.items() if c == 3)
        return ('TRIPLE_PAIR', main, 5)
    if _is_straight_values(values):
        return ('STRAIGHT', values[-1], 5)
    return None


def can_beat(play, last):
    """play 能否压住 last；last 为 None 表示自由出牌，恒可出"""
    if last is None:
        return True
    t1, m1, l1 = play[:3]
    t2, m2, l2 = last[:3]
    if t1 == 'ROCKET':
        return True
    if t2 == 'ROCKET':
        return False
    if t1 == 'BOMB':
        if t2 == 'BOMB':
            return m1 > m2
        return True
    if t2 == 'BOMB':
        return False
    if t1 != t2:
        return False
    if t1 == 'STRAIGHT':
        return l1 == l2 and m1 > m2   # 顺子必须等长
    return m1 > m2


def all_plays(hand):
    """
    枚举手牌中所有合法牌型（按牌型 (type, main, length) 去重，
    三带一/三带二带不同的牌视为同一牌型）。
    返回 [(type, main, length, cards), ...]
    """
    plays = []
    seen = set()
    groups = Counter(c.value for c in hand)

    def add(play):
        key = play[:3]
        if key not in seen:
            seen.add(key)
            plays.append(play)

    # 单张
    for v in groups:
        card = next(c for c in hand if c.value == v)
        add(('SINGLE', v, 1, [card]))

    # 对子 / 三张 / 炸弹
    for v, cnt in groups.items():
        cards = [c for c in hand if c.value == v]
        if cnt >= 2:
            add(('PAIR', v, 2, cards[:2]))
        if cnt >= 3:
            add(('TRIPLE', v, 3, cards[:3]))
        if cnt >= 4:
            add(('BOMB', v, 4, cards[:4]))

    # 王炸
    if groups.get(16, 0) >= 1 and groups.get(17, 0) >= 1:
        add(('ROCKET', 17, 2, [c for c in hand if c.value in (16, 17)]))

    # 顺子：3~A 连续，长度 2~5
    for length in range(2, 6):
        for start in range(3, 15 - length + 1):
            seq = list(range(start, start + length))
            if all(groups.get(v, 0) >= 1 for v in seq):
                cards = [next(c for c in hand if c.value == v) for v in seq]
                add(('STRAIGHT', seq[-1], length, cards))

    # 三带一（带任意单张，含王；不能带与三张同点数的第四张）
    for v3, cnt3 in groups.items():
        if cnt3 < 3:
            continue
        base = [c for c in hand if c.value == v3][:3]
        for v1, cnt1 in groups.items():
            if v1 == v3 or cnt1 < 1:
                continue
            kicker = next(c for c in hand if c.value == v1)
            add(('TRIPLE_ONE', v3, 4, base + [kicker]))

    # 三带二（带一对）
    for v3, cnt3 in groups.items():
        if cnt3 < 3:
            continue
        base = [c for c in hand if c.value == v3][:3]
        for v2, cnt2 in groups.items():
            if v2 == v3 or cnt2 < 2:
                continue
            pair = [c for c in hand if c.value == v2][:2]
            add(('TRIPLE_PAIR', v3, 5, base + pair))

    return plays


def legal_plays(hand, last):
    """当前可出的所有牌型；last 为 None 时返回全部牌型"""
    plays = all_plays(hand)
    if last is not None:
        plays = [p for p in plays if can_beat(p, last)]
    return plays


def random_ai(hand, last, rng):
    """AI：从所有合法牌型中等概率随机选一个；压牌时无合法牌型则返回 None（不要）"""
    plays = legal_plays(hand, last)
    if not plays:
        return None
    return rng.choice(plays)


class Game:
    """2人游戏流程管理。玩家编号：0=玩家，1=AI。"""

    def __init__(self, rng=None):
        self.rng = rng or random.Random()
        self.deck = build_deck()
        self.rng.shuffle(self.deck)
        self.hands = [[self.deck.pop() for _ in range(5)],
                      [self.deck.pop() for _ in range(5)]]
        self.starter = self.rng.choice([0, 1])
        self.turn = self.starter          # 当前行动者
        self.last_play = None             # 场上待压的牌（None=自由出牌）
        self.last_player = -1             # 最后出牌者（本轮赢家）
        self.first_pass = None            # 本轮第一个“不要”的人
        self.round_no = 1
        self.winner = None
        self.events = []                  # 事件日志
        self._over = False

    # ---------- 查询 ----------
    def current_legal_plays(self, player):
        return legal_plays(self.hands[player], self.last_play)

    def must_play(self, player):
        """该玩家是否必须出牌（自由出牌轮）"""
        return player == self.turn and self.last_play is None

    # ---------- 行动 ----------
    def player_play(self, player, play):
        if self._over:
            raise RuntimeError('游戏已结束')
        if player != self.turn:
            raise RuntimeError('还没轮到该玩家')
        t, m, ln, cards = play
        # 验证牌都在手牌中
        hand = self.hands[player]
        pool = list(hand)
        for c in cards:
            if c not in pool:
                raise ValueError('所选牌不在手牌中')
            pool.remove(c)
        # 验证牌型合法
        check = analyze(cards)
        if check is None or check[:3] != (t, m, ln):
            raise ValueError('非法牌型')
        # 验证能压
        if self.last_play is not None and not can_beat((t, m, ln), self.last_play):
            raise ValueError('压不过上一手牌')
        # 执行
        for c in cards:
            hand.remove(c)
        self.events.append(('play', player, (t, m, ln), list(cards)))
        if not hand:
            self.winner = player
            self._over = True
            self.events.append(('win', player))
            return
        self.last_play = (t, m, ln)
        self.last_player = player
        self.first_pass = None
        self.turn = 1 - player

    def player_pass(self, player):
        if self._over:
            raise RuntimeError('游戏已结束')
        if player != self.turn:
            raise RuntimeError('还没轮到该玩家')
        if self.last_play is None:
            raise ValueError('自由出牌轮不能过')
        if self.first_pass is None:
            self.first_pass = player
        self.events.append(('pass', player))
        self.turn = 1 - player
        if self.turn == self.last_player:
            self._end_round()

    def ai_action(self, player):
        """AI 一步：随机选合法牌型出牌；压牌无解则不要"""
        play = random_ai(self.hands[player], self.last_play, self.rng)
        if play is None:
            self.player_pass(player)
        else:
            self.player_play(player, play)

    # ---------- 内部 ----------
    def _end_round(self):
        """本轮无人再要牌：从第一个不要的人开始，依次补牌到5张"""
        winner_of_round = self.last_player
        order = [self.first_pass, 1 - self.first_pass]
        for p in order:
            drawn = 0
            while len(self.hands[p]) < 5 and self.deck:
                self.hands[p].append(self.deck.pop())
                drawn += 1
            self.events.append(('draw', p, drawn))
        self.events.append(('round_end', winner_of_round, self.first_pass))
        self.round_no += 1
        self.last_play = None
        self.last_player = -1
        self.first_pass = None
        self.turn = winner_of_round


def simulate(rng, max_steps=20000):
    """AI vs AI 模拟对局，返回 (Game, steps)"""
    g = Game(rng)
    steps = 0
    while not g._over:
        if steps > max_steps:
            raise RuntimeError('对局超过最大步数，疑似死循环')
        g.ai_action(g.turn)
        steps += 1
    return g, steps


# ============================================================
# 测试
# ============================================================
def _test_analyze():
    cases = [
        ([Card(7, 'S')], ('SINGLE', 7, 1)),
        ([Card(7, 'S'), Card(7, 'H')], ('PAIR', 7, 2)),
        ([Card(16, None), Card(17, None)], ('ROCKET', 17, 2)),
        ([Card(7, 'S'), Card(8, 'H')], ('STRAIGHT', 8, 2)),
        ([Card(7, 'S'), Card(7, 'H'), Card(7, 'C')], ('TRIPLE', 7, 3)),
        ([Card(7, 'S'), Card(8, 'H'), Card(9, 'C')], ('STRAIGHT', 9, 3)),
        ([Card(7, 'S'), Card(7, 'H'), Card(7, 'C'), Card(7, 'D')], ('BOMB', 7, 4)),
        ([Card(7, 'S'), Card(7, 'H'), Card(7, 'C'), Card(9, 'D')], ('TRIPLE_ONE', 7, 4)),
        ([Card(7, 'S'), Card(8, 'H'), Card(9, 'C'), Card(10, 'D')], ('STRAIGHT', 10, 4)),
        ([Card(7, 'S'), Card(7, 'H'), Card(7, 'C'), Card(9, 'D'), Card(9, 'S')], ('TRIPLE_PAIR', 7, 5)),
        ([Card(7, 'S'), Card(8, 'H'), Card(9, 'C'), Card(10, 'D'), Card(11, 'S')], ('STRAIGHT', 11, 5)),
        # 非法牌型
        ([Card(7, 'S'), Card(7, 'H'), Card(9, 'C')], None),
        ([Card(7, 'S'), Card(8, 'H'), Card(8, 'C'), Card(9, 'D')], None),
        ([Card(7, 'S'), Card(8, 'H'), Card(9, 'C'), Card(9, 'D'), Card(10, 'S')], None),
        ([Card(15, 'S'), Card(16, None)], None),            # 2+小王不是顺子
        ([Card(15, 'S'), Card(17, None)], None),
        ([Card(14, 'S'), Card(15, 'H')], None),             # A+2 不是顺子
        ([Card(3, 'S'), Card(4, 'H'), Card(5, 'C'), Card(6, 'D'), Card(7, 'S')], ('STRAIGHT', 7, 5)),
        ([Card(10, 'S'), Card(11, 'H'), Card(12, 'C'), Card(13, 'D'), Card(14, 'S')], ('STRAIGHT', 14, 5)),
        ([Card(13, 'S'), Card(14, 'H'), Card(15, 'C')], None),  # 含2不是顺子
    ]
    for cards, expect in cases:
        got = analyze(cards)
        assert got == expect, f'analyze 失败: {cards} -> {got}, 期望 {expect}'
    print('[OK] analyze 测试通过')


def _test_can_beat():
    P = lambda t, m, l: (t, m, l)
    # 单张
    assert can_beat(P('SINGLE', 8, 1), P('SINGLE', 7, 1))
    assert not can_beat(P('SINGLE', 7, 1), P('SINGLE', 8, 1))
    assert can_beat(P('SINGLE', 16, 1), P('SINGLE', 15, 1))
    assert can_beat(P('SINGLE', 17, 1), P('SINGLE', 16, 1))
    # 对子 / 三张
    assert can_beat(P('PAIR', 9, 2), P('PAIR', 8, 2))
    assert not can_beat(P('TRIPLE', 9, 3), P('PAIR', 8, 2))      # 类型不同
    # 顺子等长
    assert can_beat(P('STRAIGHT', 9, 3), P('STRAIGHT', 8, 3))
    assert not can_beat(P('STRAIGHT', 9, 4), P('STRAIGHT', 8, 3))  # 长度不同
    assert not can_beat(P('STRAIGHT', 8, 3), P('STRAIGHT', 8, 3))  # 相等不能压
    # 三带一/三带二按三张点数
    assert can_beat(P('TRIPLE_ONE', 10, 4), P('TRIPLE_ONE', 9, 4))
    assert can_beat(P('TRIPLE_PAIR', 10, 5), P('TRIPLE_PAIR', 9, 5))
    assert not can_beat(P('TRIPLE_PAIR', 9, 5), P('TRIPLE_ONE', 9, 4))  # 类型不同
    # 炸弹
    assert can_beat(P('BOMB', 6, 4), P('SINGLE', 17, 1))
    assert can_beat(P('BOMB', 8, 4), P('BOMB', 6, 4))
    assert not can_beat(P('BOMB', 6, 4), P('BOMB', 8, 4))
    assert can_beat(P('BOMB', 6, 4), P('TRIPLE_PAIR', 14, 5))
    # 王炸
    assert can_beat(P('ROCKET', 17, 2), P('BOMB', 15, 4))
    assert not can_beat(P('BOMB', 15, 4), P('ROCKET', 17, 2))
    # 自由出牌
    assert can_beat(P('SINGLE', 3, 1), None)
    print('[OK] can_beat 测试通过')


def _test_all_plays():
    rng = random.Random(42)
    deck = build_deck()
    for _ in range(2000):
        rng.shuffle(deck)
        hand = deck[:5]
        plays = all_plays(hand)
        assert len(plays) > 0, '手牌非空必有单张可出'
        seen = set()
        for p in plays:
            key = (p[0], p[1], p[2])
            assert key not in seen, f'牌型重复: {key}'
            seen.add(key)
            check = analyze(p[3])
            assert check is not None and check[:3] == key, \
                f'枚举牌型 {key} 无法通过 analyze 验证: {p[3]}'
            # 牌都来自手牌
            pool = list(hand)
            for c in p[3]:
                assert c in pool
                pool.remove(c)
    print('[OK] all_plays 枚举测试通过（2000手牌）')


def _test_game_flow():
    """构造特定场景验证补牌顺序与胜负判定"""
    # 场景1：玩家出牌（出对子，剩3张），AI不要 → 从AI（第一个不要的）开始补牌
    g = Game(random.Random(1))
    # 直接控制内部状态
    g.deck = [Card(v, 'S') for v in range(3, 15)]
    p_hand = [Card(3, 'S'), Card(3, 'H'), Card(5, 'C'), Card(6, 'D'), Card(7, 'S')]
    a_hand = [Card(8, 'S'), Card(9, 'H'), Card(10, 'C'), Card(11, 'D'), Card(12, 'S')]
    g.hands = [p_hand, a_hand]
    g.turn, g.last_play, g.last_player, g.first_pass = 0, None, -1, None
    g.starter = 0
    g.player_play(0, ('PAIR', 3, 2, p_hand[:2]))   # 玩家出对3
    assert g.turn == 1 and len(g.hands[0]) == 3
    g.player_pass(1)                                # AI 不要
    # 补牌：先 AI 后玩家
    assert len(g.hands[1]) == 5 and len(g.hands[0]) == 5
    assert g.turn == 0 and g.last_play is None          # 玩家赢本轮，先出
    assert g.round_no == 2
    print('[OK] 场景1：补牌顺序（第一个不要的人先补）与轮次流转通过')

    # 场景2：出完手牌立即获胜（无需对方表态、无需补牌）
    g = Game(random.Random(2))
    g.hands = [[Card(3, 'S'), Card(3, 'H'), Card(3, 'C'), Card(3, 'D')],
               [Card(4, 'S'), Card(4, 'H'), Card(4, 'C'), Card(9, 'S'), Card(9, 'H')]]
    g.turn, g.last_play, g.last_player, g.first_pass = 0, None, -1, None
    g.player_play(0, ('BOMB', 3, 4, g.hands[0][:]))
    assert g._over and g.winner == 0
    assert len(g.hands[0]) == 0
    print('[OK] 场景2：出完手牌立即获胜通过')


def _test_simulate():
    for seed in range(600):
        rng = random.Random(seed)
        g, steps = simulate(rng)
        assert g.winner in (0, 1), f'seed={seed} 无胜者'
        # 不变量：手牌 ≤ 5；出牌数守恒
        total_cards = len(g.hands[0]) + len(g.hands[1]) + len(g.deck)
        for ev in g.events:
            if ev[0] == 'play':
                total_cards += ev[2][2]
        assert total_cards == 54, f'seed={seed} 牌数不守恒: {total_cards}'
        assert len(g.hands[0]) <= 5 and len(g.hands[1]) <= 5, f'seed={seed} 手牌超5张'
        # 胜者手牌应为空
        assert len(g.hands[g.winner]) == 0, f'seed={seed} 胜者手牌非空'
        # 败者手牌非空（否则双胜，不可能）
        assert len(g.hands[1 - g.winner]) > 0
        if seed % 200 == 199:
            print(f'  [进度] 流程测试 {seed + 1}/600 局', flush=True)
    print('[OK] 600局流程测试通过（牌数守恒/手牌上限/胜负判定）')
    # 统计胜率与对局长度
    w0 = w1 = 0
    total_steps = 0
    max_steps = 0
    for seed in range(3000):
        g, steps = simulate(random.Random(seed + 100000))
        total_steps += steps
        max_steps = max(max_steps, steps)
        if g.winner == 0:
            w0 += 1
        else:
            w1 += 1
        if seed % 500 == 499:
            print(f'  [进度] 统计对局 {seed + 1}/3000 局', flush=True)
    print(f'[OK] 额外3000局统计：玩家胜 {w0}，AI 胜 {w1}（胜率 {w0/30:.1f}%），'
          f'平均步数 {total_steps/3000:.1f}，最长对局 {max_steps} 步')


def _test_draw_order_direct():
    """直接用事件流验证补牌细节：出牌者赢本轮后，对方先补、出牌者后补"""
    g = Game(random.Random(7))
    g.deck = [Card(v, 'H') for v in range(3, 18)] + [Card(v, 'D') for v in range(3, 15)]
    # 玩家手牌3张（不足5），AI手牌5张
    g.hands = [[Card(3, 'S'), Card(3, 'H'), Card(7, 'D')],
               [Card(9, 'S'), Card(9, 'H'), Card(10, 'C'), Card(11, 'D'), Card(12, 'S')]]
    g.turn, g.last_play, g.last_player, g.first_pass = 0, None, -1, None
    g.player_play(0, ('PAIR', 3, 2, g.hands[0][:2]))
    g.player_pass(1)  # AI 不要
    # 补牌顺序：AI 先补（已满，补0张），玩家后补（1张→5张，补4张）
    draws = [ev for ev in g.events if ev[0] == 'draw']
    assert draws == [('draw', 1, 0), ('draw', 0, 4)], draws
    assert len(g.hands[0]) == 5 and len(g.hands[1]) == 5
    # 牌堆不足时：补到牌堆空为止
    g2 = Game(random.Random(8))
    g2.deck = [Card(v, 'H') for v in range(3, 5)]   # 只剩2张
    g2.hands = [[Card(3, 'S'), Card(3, 'H'), Card(7, 'D')],
                [Card(9, 'S'), Card(9, 'H'), Card(10, 'C'), Card(11, 'D'), Card(12, 'S')]]
    g2.turn, g2.last_play, g2.last_player, g2.first_pass = 0, None, -1, None
    g2.player_play(0, ('PAIR', 3, 2, g2.hands[0][:2]))
    g2.player_pass(1)
    draws2 = [ev for ev in g2.events if ev[0] == 'draw']
    assert draws2 == [('draw', 1, 0), ('draw', 0, 2)], draws2  # AI已满补0，玩家补2后牌堆空
    assert len(g2.hands[1]) == 5 and len(g2.hands[0]) == 3
    print('[OK] 补牌细节测试通过（含牌堆耗尽场景）')


if __name__ == '__main__':
    _test_analyze()
    _test_can_beat()
    _test_all_plays()
    _test_game_flow()
    _test_draw_order_direct()
    _test_simulate()
    print('\n全部核心逻辑测试通过 OK')
