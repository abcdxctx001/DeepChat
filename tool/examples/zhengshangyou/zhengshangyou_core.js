/* ============================================================
 * 改版争上游（2人版）核心逻辑（浏览器与 Node 通用）
 * 规则：
 *  1. 1副54张扑克，2人对战；每人手牌上限5张，开局各抽5张。
 *  2. 牌型与普通争上游一致，顺子由5张起步改为2张起步。
 *     有效牌型：单张/对子/三张/三带一/三带二/顺子(2~5张)/炸弹/王炸
 *  3. 若某人出牌后对方不要，从第一个不要的人开始依次补牌到5张。
 *  4. 手牌率先出完者胜，无论牌堆是否还有剩余牌。
 * 点数：3<...<10<J<Q<K<A<2<小王<大王；顺子为3~A连续（不含2与王）。
 * ============================================================ */
(function (global) {
  'use strict';

  const VALUE_NAMES = { 3: '3', 4: '4', 5: '5', 6: '6', 7: '7', 8: '8', 9: '9',
    10: '10', 11: 'J', 12: 'Q', 13: 'K', 14: 'A', 15: '2' };
  const SUIT_SYMBOLS = { S: '♠', H: '♥', C: '♣', D: '♦' };

  /** 构造一副54张牌：普通牌 {v:3..15, s:'S'/'H'/'C'/'D'}，王 {v:16/17, s:null} */
  function buildDeck() {
    const deck = [];
    for (let v = 3; v <= 15; v++) {
      for (const s of ['S', 'H', 'C', 'D']) deck.push({ v, s });
    }
    deck.push({ v: 16, s: null }); // 小王
    deck.push({ v: 17, s: null }); // 大王
    return deck;
  }

  function cardName(c) {
    if (c.v === 16) return '小王';
    if (c.v === 17) return '大王';
    return SUIT_SYMBOLS[c.s] + VALUE_NAMES[c.v];
  }

  /** 分析一手牌，返回 {type, main, len} 或 null（非法牌型） */
  function analyze(cards) {
    const n = cards.length;
    if (n < 1 || n > 5) return null;
    const vs = cards.map(c => c.v).sort((a, b) => a - b);
    const cnt = {};
    vs.forEach(v => { cnt[v] = (cnt[v] || 0) + 1; });
    const vals = Object.keys(cnt).map(Number);

    const isStraight = () => {
      if (n < 2 || n > 5) return false;
      if (vs.some(v => v > 14 || v < 3)) return false;
      if (new Set(vs).size !== n) return false;
      return vs[n - 1] - vs[0] === n - 1;
    };

    if (n === 1) return { type: 'SINGLE', main: vs[0], len: 1 };
    if (n === 2) {
      if (vs[0] === 16 && vs[1] === 17) return { type: 'ROCKET', main: 17, len: 2 };
      if (vs[0] === vs[1]) return { type: 'PAIR', main: vs[0], len: 2 };
      if (isStraight()) return { type: 'STRAIGHT', main: vs[1], len: 2 };
      return null;
    }
    if (n === 3) {
      if (vals.length === 1) return { type: 'TRIPLE', main: vs[0], len: 3 };
      if (isStraight()) return { type: 'STRAIGHT', main: vs[n - 1], len: 3 };
      return null;
    }
    if (n === 4) {
      if (vals.length === 1) return { type: 'BOMB', main: vs[0], len: 4 };
      const triples = vals.filter(v => cnt[v] === 3);
      if (triples.length === 1 && vals.length === 2) return { type: 'TRIPLE_ONE', main: triples[0], len: 4 };
      if (isStraight()) return { type: 'STRAIGHT', main: vs[n - 1], len: 4 };
      return null;
    }
    // n === 5
    const triples = vals.filter(v => cnt[v] === 3);
    const pairs = vals.filter(v => cnt[v] === 2);
    if (triples.length === 1 && pairs.length === 1) return { type: 'TRIPLE_PAIR', main: triples[0], len: 5 };
    if (isStraight()) return { type: 'STRAIGHT', main: vs[n - 1], len: 5 };
    return null;
  }

  /** play 能否压住 last；last 为 null 表示自由出牌，恒可出 */
  function canBeat(play, last) {
    if (!last) return true;
    if (play.type === 'ROCKET') return true;
    if (last.type === 'ROCKET') return false;
    if (play.type === 'BOMB') {
      if (last.type === 'BOMB') return play.main > last.main;
      return true;
    }
    if (last.type === 'BOMB') return false;
    if (play.type !== last.type) return false;
    if (play.type === 'STRAIGHT') return play.len === last.len && play.main > last.main;
    return play.main > last.main;
  }

  /**
   * 枚举手牌中所有合法牌型（按牌型 type/main/len 去重，
   * 三带一/三带二带不同的牌视为同一牌型）。
   * 返回 [{type, main, len, cards}]
   */
  function allPlays(hand) {
    const plays = [];
    const seen = new Set();
    const add = (type, main, len, cards) => {
      const key = type + '|' + main + '|' + len;
      if (!seen.has(key)) {
        seen.add(key);
        plays.push({ type, main, len, cards });
      }
    };
    const cnt = {};
    hand.forEach(c => { cnt[c.v] = (cnt[c.v] || 0) + 1; });
    const vals = Object.keys(cnt).map(Number);

    // 单张
    for (const v of vals) add('SINGLE', v, 1, [hand.find(c => c.v === v)]);
    // 对子 / 三张 / 炸弹
    for (const v of vals) {
      const cs = hand.filter(c => c.v === v);
      if (cs.length >= 2) add('PAIR', v, 2, cs.slice(0, 2));
      if (cs.length >= 3) add('TRIPLE', v, 3, cs.slice(0, 3));
      if (cs.length >= 4) add('BOMB', v, 4, cs.slice(0, 4));
    }
    // 王炸
    if (cnt[16] && cnt[17]) add('ROCKET', 17, 2, hand.filter(c => c.v === 16 || c.v === 17));
    // 顺子 2~5 张（3~A 连续）
    for (let len = 2; len <= 5; len++) {
      for (let st = 3; st <= 14 - len + 1; st++) {
        let ok = true;
        const seq = [];
        for (let i = 0; i < len; i++) {
          if (!cnt[st + i]) { ok = false; break; }
          seq.push(st + i);
        }
        if (ok) add('STRAIGHT', seq[len - 1], len, seq.map(v => hand.find(c => c.v === v)));
      }
    }
    // 三带一（带任意单张，含王）
    for (const v3 of vals) {
      if (cnt[v3] < 3) continue;
      const base = hand.filter(c => c.v === v3).slice(0, 3);
      for (const v1 of vals) {
        if (v1 === v3 || cnt[v1] < 1) continue;
        add('TRIPLE_ONE', v3, 4, base.concat([hand.find(c => c.v === v1)]));
      }
    }
    // 三带二（带一对）
    for (const v3 of vals) {
      if (cnt[v3] < 3) continue;
      const base = hand.filter(c => c.v === v3).slice(0, 3);
      for (const v2 of vals) {
        if (v2 === v3 || cnt[v2] < 2) continue;
        add('TRIPLE_PAIR', v3, 5, base.concat(hand.filter(c => c.v === v2).slice(0, 2)));
      }
    }
    return plays;
  }

  /** 当前可出的牌型；last 为 null 时返回全部牌型 */
  function legalPlays(hand, last) {
    const plays = allPlays(hand);
    if (last) return plays.filter(p => canBeat(p, last));
    return plays;
  }

  /** 随机选一个牌型 */
  function randomPlay(plays) {
    return plays[Math.floor(Math.random() * plays.length)];
  }

  /** 创建一局新游戏 */
  function createGame() {
    const deck = buildDeck();
    // Fisher-Yates 洗牌
    for (let i = deck.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [deck[i], deck[j]] = [deck[j], deck[i]];
    }
    const hands = [[], []];
    for (let i = 0; i < 5; i++) {
      hands[0].push(deck.pop());
      hands[1].push(deck.pop());
    }
    return {
      deck,
      hands,
      starter: Math.floor(Math.random() * 2), // 0=玩家先手, 1=AI先手
      turn: -1,          // 当前行动者
      lastPlay: null,    // 场上待压的牌（null=自由出牌）
      lastPlayer: -1,    // 最后出牌者（本轮赢家）
      firstPass: null,   // 本轮第一个“不要”的人
      roundNo: 1,
      winner: null,
      over: false,
      log: []
    };
  }

  /** 当前玩家可出的牌型 */
  function gameLegalPlays(g, player) {
    return legalPlays(g.hands[player], g.lastPlay);
  }

  /** 出牌；返回事件字符串，供 UI 展示 */
  function applyPlay(g, player, play) {
    for (const c of play.cards) {
      const idx = g.hands[player].indexOf(c);
      if (idx >= 0) g.hands[player].splice(idx, 1);
    }
    g.log.push({ kind: 'play', player, play: { type: play.type, main: play.main, len: play.len }, cards: play.cards.slice() });
    if (g.hands[player].length === 0) {
      g.winner = player;
      g.over = true;
      g.log.push({ kind: 'win', player });
      return 'win';
    }
    g.lastPlay = { type: play.type, main: play.main, len: play.len };
    g.lastPlayer = player;
    g.firstPass = null;
    g.turn = 1 - player;
    return 'play';
  }

  /** 不要；若轮到最后的出牌者则结束本轮并补牌。返回事件 */
  function applyPass(g, player) {
    if (g.firstPass === null) g.firstPass = player;
    g.log.push({ kind: 'pass', player });
    g.turn = 1 - player;
    if (g.turn === g.lastPlayer) {
      // 本轮结束：从第一个不要的人开始依次补牌到5张
      const winnerOfRound = g.lastPlayer;
      const order = [g.firstPass, 1 - g.firstPass];
      const draws = [];
      for (const p of order) {
        let n = 0;
        while (g.hands[p].length < 5 && g.deck.length > 0) {
          g.hands[p].push(g.deck.pop());
          n++;
        }
        draws.push({ player: p, n });
        g.log.push({ kind: 'draw', player: p, n });
      }
      g.log.push({ kind: 'round_end', winnerOfRound, firstPass: g.firstPass, draws });
      g.roundNo++;
      g.lastPlay = null;
      g.lastPlayer = -1;
      g.firstPass = null;
      g.turn = winnerOfRound; // 赢家先出
      return 'round_end';
    }
    return 'pass';
  }

  /** AI 一步：随机出所有合法牌型中的任意一种；压牌无解则不要 */
  function aiAct(g, player) {
    const plays = gameLegalPlays(g, player);
    if (g.lastPlay === null) {
      // 自由出牌：必须出
      return applyPlay(g, player, randomPlay(plays));
    }
    if (plays.length === 0) {
      return applyPass(g, player);
    }
    return applyPlay(g, player, randomPlay(plays));
  }

  /** AI vs AI 模拟对局（Node 测试用） */
  function simulate(maxSteps = 5000) {
    const g = createGame();
    g.turn = g.starter;
    let steps = 0;
    while (!g.over) {
      if (steps > maxSteps) throw new Error('对局超过最大步数');
      aiAct(g, g.turn);
      steps++;
    }
    return { winner: g.winner, steps, game: g };
  }

  const ZSY = {
    buildDeck, cardName, analyze, canBeat, allPlays, legalPlays,
    randomPlay, createGame, gameLegalPlays, applyPlay, applyPass, aiAct, simulate
  };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = ZSY;
  } else {
    global.ZSY = ZSY;
  }
})(typeof window !== 'undefined' ? window : globalThis);
