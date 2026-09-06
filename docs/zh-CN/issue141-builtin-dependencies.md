# DLR 内置依赖源

内置依赖库长期保存控制节点上的安装材料，覆盖 Python wheel、JavaScript npm tgz 和 Java Maven JAR/POM/metadata。它不是公网镜像，也不会自动补齐缺包。

## 部署与使用

1. 备份后执行 `alembic upgrade head`，升级 Control 和 Worker。迁移增加三个**非默认**内置源，保留已有默认选择。旧 Worker 可以继续处理外部源任务；内置任务要求 Worker 宣告 `builtin_packages_v1`。
2. Compose 自动挂载 `dlr_builtin_packages` 到 Control 的 `/var/lib/dlr/builtin-packages`。独立部署通过 `DLR_BUILTIN_PACKAGE_ROOT` 指定本地持久目录；所有 Control 进程必须看到同一文件系统并支持 POSIX `flock`、原子 rename 和 fsync。
3. 在“系统设置 → 依赖源 → 内置依赖库”批量上传材料。Maven 可选择仓库根目录文件夹，上传时保留其下面的仓库相对路径；单独选文件时填写 `group/path/artifact/version` 前缀。
4. 明确将相应语言设为内置默认源。新接受的任务冻结该类别当前材料清单与 SHA-256，安装时只使用此快照。切换回外部源仍使用现有“来源配置”。
5. 选择已保存的适配器和目标 Worker 检查可安装性。检查使用真实队列、Attempt、超时与资源额度，准备依赖环境但不执行业务脚本、不注入业务凭据。Java 会编译已保存代码，所以代码编译错误也会导致检查失败。检查结果记录在执行历史。

“已上传”只说明类型、元数据与内容身份校验通过。可安装性检查分别显示缺少依赖、不匹配 Worker 环境及其他失败；原始安装工具日志可在该适配器的执行历史查看。

## 离线准备材料

在与目标 Worker 相同的系统、架构和运行时环境中准备并演练安装。当前 Worker 使用 Python 3.13、Node.js 22、Java 21，具体版本以交付镜像为准。

- **Python**：准备所有直接及传递依赖的 wheel，例如在相同环境中使用 `pip download --only-binary=:all: -r requirements.txt -d wheelhouse`。只接收 wheel，不执行源码构建。检查 Python ABI、平台标签与 `Requires-Python`。requirements 不允许索引参数、路径或直接 URL，wheel 元数据也不得含直接下载依赖。
- **npm**：逐项准备完整依赖闭包的 `npm pack` tgz；运行时由 npm 使用内存中的本地只读 registry 元数据解析版本范围。必须包含 optional/peer 依赖实际需要的材料。拒绝外部 URL、Git/本地路径依赖、bundled node_modules 与内嵌 npm-shrinkwrap；安装使用 `--ignore-scripts`，需要 postinstall 构建或下载的包不在此材料格式的可用范围内。准备相应平台的预构建包并实测。
- **Maven**：在干净的本地仓库中用 Adapter 对应依赖的 POM 执行 `mvn -Dmaven.repo.local=/path/to/repository org.apache.maven.plugins:maven-dependency-plugin:3.8.1:copy-dependencies -DoutputDirectory=/path/to/deps`。上传仓库中的 JAR、POM 和必要 metadata XML，包含父 POM、BOM、传递依赖以及 dependency-plugin 3.8.1 自身和它的依赖。忽略 `.lastUpdated`、`_remote.repositories` 与校验旁车文件。只上传 SDK JAR 不足以组成可安装仓库。未解析的 POM 坐标属性须先准备成可验证的发布 POM。

Worker 始终调用 uv/npm/Maven；Python 使用严格离线 wheelhouse，npm 只访问该 Attempt 的回环 registry，Maven 使用独立仓库和 `-o`。下载由带当前 Claim 凭据的 Worker 完成，再验证长度及 SHA-256；包管理器不接收管理员 Token 或 Worker Token。缓存身份包含材料快照与运行时身份，已有校验通过的环境可以复用，不必每次重新下载。

## 容量、删除与占用

默认三种语言合计 **1 GiB（1,073,741,824 字节）**，管理员可调整。限制同时计算已保存字节、所有上传预留，以及 `DLR_BUILTIN_PACKAGE_MIN_FREE_BYTES` 磁盘安全余量（默认 256 MiB）。Worker 安装缓存与解压空间不计入此 Control 配额，仍受 Worker 资源限制。

中断或失败的上传继续占用预留，可在列表中释放；后台必须确认没有活跃上传并实际移除临时正文后才释放。并发操作通过数据库容量锁及文件锁协调，不自动删包或按附件 TTL 过期。目录中可能保留不含正文的空锁文件。

删除需要确认，并阻止下载中、未完成任务以及清理状态不确定的引用。未知清理状态应先通过现有 Worker 清理与运行恢复流程确认，不能直接修改数据库解除占用。正文删除并 fsync 成功后才返回成功；删除失败保留“删除失败 / 待重试”状态，可再次删除。删除后下载入口失效，不保留可恢复正文副本。

删除控制节点材料不会卸载 Worker 已安装环境；新 Worker 或缓存回收后可能无法重建。材料集合变化会改变新任务的缓存身份，可能触发重建。这不是禁用或撤销包的能力。

## 备份恢复

备份必须同时包含 PostgreSQL（材料元数据、容量、预留、任务快照）和 `DLR_BUILTIN_PACKAGE_ROOT` 全目录（`.blob`、`.part` 与锁文件）。先停止新任务和库写入，等待活跃任务/上传/下载停止，再停止 Control 与 Worker，取得一致备份。按同一时间点恢复数据库与目录，保持所有者、权限和持久挂载，再启动服务。

恢复后核对材料大小和 SHA-256、预留与正文对应关系，并在每种 Worker 环境执行安装检查。不要从不同时间点拼接数据库与目录，也不要只恢复元数据。无法确认的预留仍计费；页面释放会实际删除其临时/孤立正文。不要在运行中手工移除文件或调整数据库状态。
