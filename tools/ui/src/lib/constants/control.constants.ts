/**
 * Local-control integration constants (fork Phase 1).
 *
 * The control service is a separate origin on purpose: it must stay reachable
 * whether the SPA is served by the control service itself, by a llama-server
 * --path mount, or during vite dev — one absolute base, no env plumbing.
 */
export const CONTROL_BASE = 'http://127.0.0.1:8098';

/** Route id (page.route.id) for the Local AI page — matches active checks. */
export const CONTROL_ROUTE_ID = '/control';
/**
 * Hash URL for links/goto: llama-ui runs `router: { type: 'hash' }`
 * (see svelte.config.js), so navigation targets look like '#/control'
 * — same pattern as ROUTES.SEARCH = '#/search'.
 */
export const CONTROL_ROUTE = '#/control';

/** Runtime list poll cadence on the Local AI page. */
export const CONTROL_POLL_MS = 3000;

/** Human labels for the compute devices runtimes are configured for. */
export const DEVICE_LABELS: Record<string, string> = {
	cpu: 'CPU',
	default: 'Server default',
	gpu: 'GPU · Adreno',
	hybrid: 'Hybrid',
	multi: 'Multi-backend',
	npu: 'NPU · Hexagon',
	vulkan: 'Vulkan'
};
