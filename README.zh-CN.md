# gitws

`gitws` 是一个简单、可控的本地 Git 工作区管理工具，用于管理仓库版本、
隔离工作区、共享 Git 缓存和磁盘占用。

英文文档见 [README.md](README.md)。

## 解决的问题

多个任务处理大型仓库时，反复 clone 会重复占用 Git 历史、Git LFS 和子仓库
对象。`gitws` 在原始仓库之外维护 mirror，并从固定 commit 创建隔离工作区。

主仓库和递归子仓库的 Git 对象共享，源码文件和编译产物仍然各自独立。

## 安装

```bash
python3 -m pip install "git+https://github.com/cexianl/gitws.git"
gitws --help
```

如果系统启用了 PEP 668：

```bash
python3 -m venv .venv
.venv/bin/pip install -e /path/to/gitws
ln -sfn "$PWD/.venv/bin/gitws" "$HOME/.local/bin/gitws"
```

默认状态目录是 `~/.gitws`，也可以通过 `GITWS_HOME` 修改。

## 基本用法

```bash
gitws add project /path/to/project
gitws sync project
gitws switch project 5323e011d
gitws new project task-a
gitws new project task-b --at def456
gitws status project
gitws size project
gitws doctor project
gitws remove task-a
gitws clean ptj1
```

## 与原生 `git worktree` 的区别

| 能力 | `git worktree` | `gitws` |
| --- | --- | --- |
| 创建多个工作区 | 支持 | 支持 |
| 共享主仓库 Git 对象 | 支持 | 通过外部 mirror 支持 |
| 固定工作区 commit | 支持 | 支持，并记录清单 |
| 仓库别名 | 不支持 | 支持 |
| 同步或切换原始仓库 | 不负责 | `sync`、`switch` |
| 递归初始化子仓库 | 手动执行 | 自动执行 |
| 共享子仓库 Git 对象 | 手动配置 | 自动建立 reference mirror |
| 状态和磁盘统计 | 不支持 | `status`、`size` |
| 多进程协调 | 不负责 | Linux 文件锁 |

`gitws` 使用系统 Git 命令完成仓库操作，Git 配置、SSH 配置和认证仍由用户控制。

## 安全边界

- `add` 只记录原始仓库路径、远程地址和默认分支。
- 只有 `sync` 和 `switch` 会修改原始仓库。
- 修改原始仓库前会检查未提交修改和子仓库状态。
- `new` 在 `~/.gitws/workspaces` 下创建 detached 工作区。
- 工作区固定主仓库和递归子仓库 commit。
- 当前版本面向 Linux，使用 `fcntl` 文件锁。


