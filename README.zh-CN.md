# reclaim

macOS 磁盘体检与安全清理 —— 一条命令给人和 agent 用。

`reclaim` 只回答三个问题，不猜：

1. **谁在吃盘** —— 按真实占用算，稀疏文件不会被夸大。
2. **哪些能删** —— 每一项都带风险等级和原因。
3. **哪些可以无人值守跑** —— 只有 `safe`（可再生）档允许自动执行。

[English](README.md)

## 为什么再造一个

现有工具要么只报数字不给判断（`du`、`ncdu`），要么把判断藏在 GUI 里。
agent 需要的是**判断本身**：带风险等级、稳定 id、默认拒绝破坏性操作的机器可读计划。

`reclaim` 复用你 Mac 上已有的工具（`du`、`docker`、`brew`、`xcrun`、`mdfind`、`git`），
不重复实现它们。

## 安装

```bash
git clone https://github.com/justin-mc-lai/reclaim.git
cd reclaim
pipx install .
# 或者直接：python3 reclaim.py <命令>
```

需要 macOS + Python 3.9+，无第三方依赖。

## 用法

```bash
reclaim scan                       # $HOME 下最大的目录
reclaim plan                       # 带风险标签的清理计划
reclaim plan --deep                # 追加 dev 构建产物和 git worktree（慢）
reclaim plan --json                # 给 agent 读
reclaim clean --safe               # 试运行：只告诉你会做什么
reclaim clean --safe --yes         # 只执行 risk=safe 的项
reclaim clean --ids 5f45318746 --yes
reclaim clean --ids <id> --yes --force   # dangerous 必须加 --force
```

## 风险模型

| 等级 | 含义 | 例子 |
|---|---|---|
| `safe` | 按需再生，不含用户数据 | 应用缓存、Xcode DerivedData、悬空 Docker 镜像、包管理器缓存、不可用模拟器 |
| `review` | 能重建或重下，但要花时间/带宽 | `node_modules`、`dist`、`target`、大视频、磁盘镜像、干净的 git worktree |
| `dangerous` | 可能丢未提交的工作 | 有改动的 git worktree |

`clean` 默认是试运行，只有传 `--yes` 才真的删。
`dangerous` 即使传了 `--yes` 也会被拒绝，必须再加 `--force`。

## Agent 契约

`plan --json` 返回：

```json
{
  "free_gb": 18.4,
  "items": [
    {
      "id": "5f45318746",
      "kind": "docker-images",
      "path": "",
      "bytes": 0,
      "gb": 0.0,
      "risk": "safe",
      "why": "未被任何容器引用的镜像，需要时可重新 pull",
      "cmd": ["docker", "image", "prune", "-a", "-f"],
      "note": "142.5GB (79%) reclaimable of 179.5GB"
    }
  ]
}
```

agent 应当：先读 `risk`，只对 `safe` 动手，再把 `reclaim clean --json` 的结果
（`reclaimed_gb` 和逐项结果）汇报给用户。

## 安全边界

- 删除只允许发生在 `$HOME` 内；`/`、`/System`、`/Applications`、`$HOME` 本身
  以及常用的用户目录直接拒绝。
- 当前工作目录及其所有父目录永远不会被删。
- 符号链接只解链，不跟随。
- 基于命令的清理调用厂商自己的 prune（`docker image prune`、`brew cleanup`、
  `simctl delete unavailable`），由厂商判断什么是孤儿。
- 不联网、不上报、没有常驻进程。

## 已知局限

- `plan --deep` 会对 `~/Developer` 跑一次 `du`，大目录下受 IO 限制。
- 风险等级是启发式规则。`review` 是"请你看一眼"，不是结论。
- Docker 的尺寸来自 `docker system df`；镜像文件本身是稀疏文件，所以 `ls` 和
  `du` 的数字天生不一致。

## 测试

```bash
python3 reclaim.py selftest
```

## 许可

MIT
