# gitws

[English](README.md)

为并行开发创建隔离工作区，共享 Git、子仓库和 Git LFS 对象，并可选共享源码文件，减少磁盘占用。

## 安装

需要 Linux、Python 3.10+ 和 Git。使用 Git LFS 的项目还需安装 Git LFS。

```bash
python3 -m pip install "git+https://github.com/cexianl/gitws.git"
gitws --help
```

如果系统限制 pip 安装，可用 `pipx install "git+https://github.com/cexianl/gitws.git"`。

## 使用

```bash
gitws add project /path/to/project
gitws new project task-a
cd "$(gitws path task-a)"
git switch -c task-a
```

远程仓库使用 `gitws add upstream https://github.com/example/project.git` 登记。
支持 HTTPS、SSH 和 `file://` URL。

| 来源 | 默认 commit | 数据来源 |
| --- | --- | --- |
| 本地路径 | 本地 `HEAD` | 只使用本地对象；所需子仓库和 LFS 对象须已存在 |
| 远程 URL | 远端默认分支的最新 commit | 拉取远端更新 |

`new --at <commit>` 可指定提交。工作区初始为 detached HEAD，递归子仓库固定到记录的
commit。之后更新来源仓库不会改变已有工作区。未提交修改、未跟踪文件和构建产物不会从来源目录复制。

每个任务使用不同的分支名。工作区的 `origin` 指向登记的上游仓库；本地来源未配置上游时，
工作区也不设置 `origin`。已有别名不能重新登记为不同的来源。

## 命令

| 命令 | 用途 |
| --- | --- |
| `add <name> <path-or-url>` | 登记仓库别名 |
| `new <name> <workspace> [--at COMMIT] [--cow]` | 创建工作区 |
| `path <workspace>` | 输出工作区路径 |
| `list` | 列出仓库和工作区 |
| `status [name]` | 查看 commit 和修改状态 |
| `usage <name> [--bytes]` | 查看磁盘占用 |
| `remove <workspace>` | 删除工作区，包括其中的本地修改 |
| `mount <workspace>` | 恢复 COW 挂载，例如重启后恢复工作区 |
| `doctor <name>` | 检查仓库和工作区完整性 |
| `clean <name> [--unused] [--dry-run]` | 清理失效记录，可选回收未使用的 COW 基础目录 |
| `sync <name>` | 将原始仓库切换到远端分支的最新 commit |
| `switch <name> <commit>` | 将原始仓库切换到指定 commit |

只有 `sync` 和 `switch` 会修改原始仓库；两者仅适用于本地路径来源，存在本地修改时会拒绝执行。

## 存储

默认目录为 `~/.gitws`，可通过环境变量修改：

```bash
export GITWS_HOME=/data/gitws
```

`gitws --home /data/gitws <command>` 的优先级高于 `GITWS_HOME`。

普通工作区各自保存源码。`new --cow` 还会共享完整源码目录，并分别保存各工作区的修改。
需要 OverlayFS 或 `fuse-overlayfs`；使用 `gitws remove` 卸载并删除工作区。
重启后，执行 `gitws mount <workspace>` 恢复目录及已有修改。

未使用的基础目录会保留以供复用，并与子仓库镜像一起计入 `usage`。预览和回收未使用的基础目录：

```bash
gitws clean project --unused --dry-run
gitws clean project --unused
```

仍被工作区引用的基础目录会保留，包括工作区尚未挂载的情况。
包含子仓库本地 Git 对象的基础目录也会保留，以保护任务提交。Git 和 LFS 对象缓存会保留。
不同项目可并发创建工作区，同一仓库的管理操作按顺序执行。

`usage` 显示文件长度（`logical`）和实际分配的磁盘块（`physical`），包括构建产物。
硬链接的物理占用只计算一次。结果逐项输出，`--bytes` 可显示精确字节数。

## 与 `git worktree` 对比

| 能力 | `git worktree` | `gitws` |
| --- | --- | --- |
| 共享主仓库 Git 对象 | 支持 | 支持 |
| 仓库别名 | 不支持 | 支持 |
| 初始化递归子仓库 | 手动 | 自动 |
| 共享子仓库和 LFS 对象 | 手动配置 | 自动 |
| 共享源码文件 | 不支持 | `--cow` |
| 磁盘占用统计 | 另用工具 | `usage` |

## 磁盘节省示例

在 Linux 上测量一个包含递归子仓库的大型项目，物理占用分别为：Git 对象 **0.92 GiB**、
LFS 对象 **0.63 GiB**、检出的源码 **6.40 GiB**。一个 COW 工作区写层占 **0.0006 GiB**，
复用已有基础目录创建工作区约需 **2 分 08 秒**。

据此估算 **5 个基于同一 commit 的工作区**：

| 方案 | 物理占用 |
| --- | ---: |
| 5 次独立 clone | 39.75 GiB |
| 5 个 `gitws --cow` 工作区 | 7.95 GiB |
| 节省 | **31.80 GiB（80%）** |

计算方式：独立 clone 为 `5 × (0.92 + 0.63 + 6.40)` GiB；COW 为
`0.92 + 0.63 + 6.40 + 5 × 0.0006` GiB。不计已有原始仓库和构建产物，任务修改及构建会增加各工作区占用。
