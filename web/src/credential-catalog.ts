/**
 * 最小跨设置凭据元数据同步（M5.5.2，UX-003）。
 *
 * 系统设置中的凭据增删改后，所有依赖凭据元数据的选择器（AI 模型设置、
 * Adapter 凭据绑定、依赖源、Webhook）无需 F5 即可看到最新状态。
 *
 * 本模块只负责"已变化"事件分发，不持有任何数据：
 * - 订阅方各自重新向服务端拉取元数据，服务端始终是唯一数据源；
 * - 凭据真值（Secret）从不进入前端共享状态，API 也只返回元数据；
 * - 刻意不引入状态管理框架，一个发布/订阅工具足以覆盖低频平台配置。
 */

type Listener = () => void;

const listeners = new Set<Listener>();

/** 订阅凭据元数据变化；返回取消订阅函数。 */
export function subscribeCredentialCatalog(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** 凭据已新建/更新/删除：通知所有订阅方重新拉取元数据。 */
export function notifyCredentialCatalogChanged(): void {
  for (const listener of [...listeners]) {
    listener();
  }
}
