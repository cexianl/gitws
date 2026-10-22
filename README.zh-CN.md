# gitws

`gitws` 是一个简单、可控的本地 Git 工作区管理工具，用于管理仓库版本、
隔离工作区、共享 Git 缓存和磁盘占用。

英文文档见 [README.md](README.md)。

## 解决的问题

多个任务处理大型仓库时，反复 clone 会重复占用 Git 历史、Git LFS 和子仓库
对象。`gitws` 在独立存储目录中维护共享 Git 对象，并从固定 commit 创建隔离工作区。

主仓库和递归子仓库的 Git 对象共享，普通工作区的源码文件和编译产物仍然各自独立。
安装了 Git LFS 时，同一项目的工作区还会共享 LFS 对象存储；工作区中的源码文件仍然独立。

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

也可以在命令中直接指定存储目录：

```bash
gitws --home /data/gitws new project task-a --cow
```

命令行中的 `--home` 优先级高于 `GITWS_HOME`。

## 基本用法

```bash
gitws add project /path/to/project
gitws add upstream https://github.com/example/project.git
gitws sync project
gitws switch project 5323e011d
gitws new project task-a
gitws new project task-b --at def456
gitws new project task-c --cow
gitws status project
gitws usage project
gitws doctor project
gitws remove task-a
gitws clean project
```

本地路径来源默认使用该仓库当前的 `HEAD`；`new` 只读取本机的 Git 对象、已初始化
子仓库和现有 LFS 对象，不访问远端。首次建立共享对象仓库会进行本地 Git 复制，
不会复制原目录中的未跟踪文件、忽略文件或构建产物。也可通过 `--at` 指定本机已有的
commit。对象缓存建立后，即使原始目录被删除，已缓存的 commit 仍可创建工作区。

URL 来源默认同步远端默认分支；可使用 HTTPS、SSH 或 `file://` URL。每次 `new`
会获取远端更新，再将工作区固定到选定 commit。`sync` 和 `switch` 仅适用于
本地路径来源，用于明确修改原始仓库。离线使用本地来源时，所需子仓库和 LFS
对象必须已在本机；缺少的对象不会从远端补取。

## 磁盘占用

`gitws usage <name>` 实时扫描文件，每统计完一项就立即输出。原始目录和工作区中的
未跟踪文件、构建产物也计入占用。对于 COW 工作区，每次命令只扫描一次各共享基础
目录，再分别统计各工作区写层；不会重复扫描挂载后的完整目录树。

`logical` 表示文件长度，`physical` 表示文件实际分配的磁盘块，跨统计项的硬链接
只计算一次物理占用。软链接只统计链接本身，稀疏文件按实际分配的磁盘块计算。
结果不缓存，因此大型目录仍需要扫描时间；同时进行的构建可能在扫描期间改变文件。
使用 `--bytes` 可以输出精确字节数。

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
| 状态和磁盘统计 | 不支持 | `status`、`usage` |
| 多进程协调 | 不负责 | Linux 文件锁 |

`gitws` 使用系统 Git 命令完成仓库操作，Git 配置、SSH 配置和认证仍由用户控制。
如果系统安装了 Git LFS，`usage` 会单独统计共享的 LFS 对象存储。
`new --cow` 会使用 OverlayFS 创建完整目录树的写时复制工作区；普通挂载不可用时会尝试使用 `fuse-overlayfs`。这是 Linux 下的可选高级功能，工作区应使用 `gitws remove` 删除，以便正确卸载挂载点。

## 安全边界

- `add` 只记录本地路径或远程 URL；不会修改来源仓库。
- 只有 `sync` 和 `switch` 会修改原始仓库。
- 修改原始仓库前会检查未提交修改和子仓库状态。
- `new` 在 `~/.gitws/workspaces` 下创建 detached 工作区。
- 工作区固定主仓库和递归子仓库 commit。
- 当前版本面向 Linux，使用 `fcntl` 文件锁。

## 实测数据示例

在 Linux 上用一个包含大量递归子仓库的大型仓库进行匿名化测试，使用
`new --cow` 得到以下结果：

| 项目 | 实测值 |
| --- | ---: |
| 共享 Git 对象 | 0.92 GiB（物理占用） |
| 共享 Git LFS 对象 | 0.63 GiB（物理占用） |
| 共享完整 tracked 源码基础目录 | 6.40 GiB（物理占用） |
| 一个工作区写层 | 0.0006 GiB（物理占用） |
| 后续创建一个 COW 工作区 | 约 2 分 08 秒 |

以 5 个并行工作区为例，只比较任务额外占用，不计算已经存在的原始仓库：

| 方案 | 5 个工作区的物理占用 |
| --- | ---: |
| 5 次独立 clone | 约 39.75 GiB |
| `gitws --cow` | 约 7.95 GiB |
| `gitws --cow` 节省 | 约 31.80 GiB（80.0%） |

独立 clone 的计算方式是：`5 ×（6.40 GiB tracked 源码 + 0.92 GiB Git
对象 + 0.63 GiB LFS 对象）`。COW 的计算方式是：一份共享 Git 对象、一份
共享 LFS 对象、一份 6.40 GiB 共享基础目录，以及 5 个小写层。写层数值
来自一次实测，实际占用会随任务修改量变化。原始 checkout 中还包含被 Git
忽略的本地 worktree 和构建文件，它们不属于 tracked 源码对比。clone 的
网络耗时受远端服务和网络影响，本示例没有把它写成固定基准。


