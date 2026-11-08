<script lang="ts">
	import { Button } from '$lib/components/ui/button';
	import { APP_NAME, CONTROL_BASE, CONTROL_POLL_MS, DEVICE_LABELS } from '$lib/constants';
	import { onMount } from 'svelte';

	/**
	 * Local AI control: start/stop/activate inference runtimes, read their
	 * logs, browse the model store and run device benchmarks — all through
	 * the local-control service (local-control/control.py, port 8098).
	 */

	interface RuntimeRow {
		id: string;
		label: string;
		kind: string | null;
		device: string | null;
		platform: string | null;
		status: 'running' | 'stopped' | 'exited';
		pid: number | null;
		exit_code: number | null;
		uptime_s: number | null;
		health: boolean | null;
		health_port: number | null;
		active: boolean;
		notes: string | null;
	}

	interface RuntimesResponse {
		runtimes: RuntimeRow[];
		active: string | null;
	}

	interface ModelRow {
		file: string;
		name: string;
		path: string;
		size_mb: number;
		ram_fit_pct: number | null;
	}

	interface ModelsResponse {
		models: ModelRow[];
		mem_available_mb: number;
	}

	interface BenchRow {
		file: string;
		device: string | null;
		device_id: string | null;
		model: string | null;
		ttft_ms: number | null;
		prefill_tps: number | null;
		decode_tps: number | null;
	}

	interface BenchResponse {
		results: BenchRow[];
		devices: string[];
	}

	let runtimes = $state<RuntimeRow[]>([]);
	let active = $state<string | null>(null);
	let models = $state<ModelRow[]>([]);
	let memMb = $state(0);
	let modelsLoaded = $state(false);
	let benchResults = $state<BenchRow[]>([]);
	let benchDevices = $state<string[]>([]);
	let error = $state<string | null>(null);
	let busy = $state<string | null>(null);
	let openLogId = $state<string | null>(null);
	let logLines = $state<string[]>([]);
	let pendingBench = $state<string | null>(null);

	let activeRuntime = $derived(runtimes.find((r) => r.id === active) ?? null);
	let chatReady = $derived(activeRuntime !== null && activeRuntime.health === true);

	async function json<T>(path: string, init?: RequestInit): Promise<T> {
		const res = await fetch(`${CONTROL_BASE}${path}`, init);

		let body: unknown = null;

		try {
			body = await res.json();
		} catch {
			// non-JSON error bodies still need the status surfaced below
		}

		if (!res.ok) {
			const message =
				(body as { error?: string } | null)?.error ?? `${res.status} ${res.statusText}`;

			throw new Error(message);
		}

		return body as T;
	}

	function post(path: string, payload?: object): Promise<unknown> {
		return json(path, {
			body: JSON.stringify(payload ?? {}),
			headers: { 'Content-Type': 'application/json' },
			method: 'POST'
		});
	}

	function report(err: unknown, context: string): void {
		error = `${context}: ${err instanceof Error ? err.message : String(err)}`;
	}

	async function refreshRuntimes(): Promise<void> {
		try {
			const data = await json<RuntimesResponse>('/ctl/runtimes');

			runtimes = data.runtimes;
			active = data.active;
			error = null;
		} catch (err) {
			report(
				err,
				'Control service unreachable — start it with the workbench Local AI launcher or `python3 local-control/control.py`'
			);
		}
	}

	async function refreshModels(): Promise<void> {
		try {
			const data = await json<ModelsResponse>('/ctl/models');

			models = data.models;
			memMb = data.mem_available_mb;
			modelsLoaded = true;
		} catch (err) {
			report(err, 'Model store unavailable');
		}
	}

	async function refreshBench(): Promise<void> {
		try {
			const data = await json<BenchResponse>('/ctl/bench');

			benchResults = data.results;
			benchDevices = data.devices;
		} catch (err) {
			report(err, 'Bench results unavailable');
		}
	}

	async function act(id: string, action: 'start' | 'stop' | 'activate'): Promise<void> {
		busy = id;
		error = null;

		try {
			await post(`/ctl/runtimes/${id}/${action}`);
			await refreshRuntimes();
		} catch (err) {
			report(err, `${action} failed for ${id}`);
		} finally {
			busy = null;
		}
	}

	async function toggleLog(id: string): Promise<void> {
		if (openLogId === id) {
			openLogId = null;

			return;
		}

		openLogId = id;

		try {
			const data = await json<{ lines: string[] }>(`/ctl/runtimes/${id}/log?tail=40`);

			logLines = data.lines;
		} catch (err) {
			logLines = [`log unavailable: ${err instanceof Error ? err.message : String(err)}`];
		}
	}

	async function runBench(device: string): Promise<void> {
		pendingBench = device;
		error = null;

		try {
			await post('/ctl/bench', { device });
			await refreshBench();
		} catch (err) {
			report(err, `Bench run on ${device} failed`);
		} finally {
			pendingBench = null;
		}
	}

	function deviceLabel(device: string | null): string {
		return (device && DEVICE_LABELS[device]) || device || '—';
	}

	function uptime(seconds: number | null): string {
		if (seconds === null) return '—';

		if (seconds < 60) return `${Math.round(seconds)}s`;

		const minutes = Math.floor(seconds / 60);
		const rest = Math.round(seconds % 60);

		return `${minutes}m ${rest}s`;
	}

	onMount(() => {
		void refreshRuntimes();
		void refreshModels();
		void refreshBench();
		const timer = setInterval(() => void refreshRuntimes(), CONTROL_POLL_MS);

		return () => clearInterval(timer);
	});
</script>

<svelte:head>
	<title>Local AI - {APP_NAME}</title>
</svelte:head>

<div class="mx-auto flex w-full max-w-7xl flex-col gap-6 px-4 pt-12 md:px-8 md:pt-6">
	<div class="flex flex-wrap items-start gap-4">
		<div class="flex min-w-0 flex-col gap-1">
			<h1 class="text-2xl font-semibold tracking-tight">Local AI</h1>

			<p class="text-sm text-muted-foreground">
				Pick the runtime and compute device for local inference — llama.cpp builds and GenieX.
			</p>
		</div>

		<div class="ml-auto flex shrink-0 items-center gap-2">
			{#if chatReady && activeRuntime}
				<Button
					href="#/"
					onclick={() => {
						window.location.hash = '/';
					}}
					size="sm"
					variant="tertiary"
				>
					Open chat on {activeRuntime.label}
				</Button>
			{/if}

			<Button onclick={() => void refreshRuntimes()} size="sm" variant="outline">Refresh</Button>
		</div>
	</div>

	{#if error}
		<div
			class="rounded-md border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive"
			data-testid="control-error"
			role="alert"
		>
			{error}
		</div>
	{/if}

	<section class="flex flex-col gap-3">
		<h2 class="text-lg font-medium">Runtimes</h2>

		<div class="overflow-x-auto rounded-lg border">
			<table class="w-full min-w-[760px] text-left text-sm" data-testid="control-runtimes">
				<thead class="bg-muted/50 text-muted-foreground">
					<tr>
						<th class="px-4 py-2 font-medium">Runtime</th>

						<th class="px-4 py-2 font-medium">Device</th>

						<th class="px-4 py-2 font-medium">Platform</th>

						<th class="px-4 py-2 font-medium">Status</th>

						<th class="px-4 py-2 font-medium">Health</th>

						<th class="px-4 py-2 font-medium">Uptime</th>

						<th class="px-4 py-2 text-right font-medium">Actions</th>
					</tr>
				</thead>

				<tbody class="divide-y">
					{#each runtimes as rt (rt.id)}
						<tr data-testid={`control-runtime-${rt.id}`}>
							<td class="px-4 py-2">
								<div class="flex items-center gap-2">
									<span class="font-medium">{rt.label}</span>

									{#if rt.active}
										<span
											class="rounded-full bg-emerald-500/15 px-2 py-0.5 text-xs text-emerald-600 dark:text-emerald-400"
										>
											active
										</span>
									{/if}
								</div>

								<button
									class="mt-0.5 text-xs text-muted-foreground underline-offset-2 hover:underline"
									onclick={() => void toggleLog(rt.id)}
									type="button"
								>
									{openLogId === rt.id ? 'hide log' : 'log'}
								</button>
							</td>

							<td class="px-4 py-2">{deviceLabel(rt.device)}</td>

							<td class="px-4 py-2">{rt.platform ?? '—'}</td>

							<td class="px-4 py-2">
								<span
									class={rt.status === 'running'
										? 'text-emerald-600 dark:text-emerald-400'
										: rt.status === 'exited'
											? 'text-destructive'
											: 'text-muted-foreground'}
								>
									{rt.status}{rt.exit_code !== null ? ` (rc ${rt.exit_code})` : ''}
								</span>
							</td>

							<td class="px-4 py-2">{rt.health === null ? '—' : rt.health ? 'up' : 'down'}</td>

							<td class="px-4 py-2">{uptime(rt.uptime_s)}</td>

							<td class="px-4 py-2">
								<div class="flex justify-end gap-1.5">
									{#if rt.status !== 'running'}
										<Button
											data-testid={`control-start-${rt.id}`}
											disabled={busy === rt.id}
											onclick={() => void act(rt.id, 'start')}
											size="sm"
											variant="tertiary"
										>
											Start
										</Button>
									{:else}
										{#if rt.health && !rt.active}
											<Button
												data-testid={`control-activate-${rt.id}`}
												disabled={busy === rt.id}
												onclick={() => void act(rt.id, 'activate')}
												size="sm"
												variant="tertiary"
											>
												Use
											</Button>
										{/if}

										<Button
											data-testid={`control-stop-${rt.id}`}
											disabled={busy === rt.id}
											onclick={() => void act(rt.id, 'stop')}
											size="sm"
											variant="outline"
										>
											Stop
										</Button>
									{/if}
								</div>
							</td>
						</tr>

						{#if openLogId === rt.id}
							<tr>
								<td class="px-4 pb-3" colspan="7">
									<pre
										class="max-h-56 overflow-auto rounded bg-muted/60 p-3 text-xs whitespace-pre-wrap">{logLines.join(
											'\n'
										)}</pre>
								</td>
							</tr>
						{/if}
					{:else}
						<tr>
							<td class="px-4 py-3 text-muted-foreground" colspan="7"> No runtimes loaded yet. </td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	</section>

	<section class="flex flex-col gap-3">
		<div class="flex items-center gap-3">
			<h2 class="text-lg font-medium">Model store</h2>

			{#if modelsLoaded}
				<span class="text-sm text-muted-foreground">
					{models.length} model{models.length === 1 ? '' : 's'} · {memMb} MB RAM available
				</span>
			{/if}
		</div>

		<div class="overflow-x-auto rounded-lg border">
			<table class="w-full min-w-[560px] text-left text-sm" data-testid="control-models">
				<thead class="bg-muted/50 text-muted-foreground">
					<tr>
						<th class="px-4 py-2 font-medium">Model</th>

						<th class="px-4 py-2 text-right font-medium">Size</th>

						<th class="px-4 py-2 text-right font-medium">RAM fit</th>
					</tr>
				</thead>

				<tbody class="divide-y">
					{#each models as model (model.path)}
						<tr>
							<td class="px-4 py-2">
								<div class="font-medium">{model.name}</div>

								<div class="text-xs break-all text-muted-foreground">{model.path}</div>
							</td>

							<td class="px-4 py-2 text-right tabular-nums">
								{model.size_mb >= 1024
									? `${(model.size_mb / 1024).toFixed(1)} GB`
									: `${model.size_mb} MB`}
							</td>

							<td class="px-4 py-2 text-right tabular-nums">
								{#if model.ram_fit_pct === null}
									—
								{:else}
									<span class={model.ram_fit_pct > 100 ? 'text-destructive' : ''}>
										{model.ram_fit_pct}%
									</span>
								{/if}
							</td>
						</tr>
					{:else}
						<tr>
							<td class="px-4 py-3 text-muted-foreground" colspan="3">
								No models found in the configured store.
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	</section>

	<section class="flex flex-col gap-3 pb-12">
		<div class="flex flex-wrap items-center gap-3">
			<h2 class="text-lg font-medium">Device benchmarks</h2>

			<span class="text-sm text-muted-foreground"> geniex-bench · Qwen3-0.6B · ~20 s per run </span>

			<div class="ml-auto flex flex-wrap gap-1.5">
				{#each benchDevices as device (device)}
					<Button
						data-testid={`control-bench-${device}`}
						disabled={pendingBench !== null}
						onclick={() => void runBench(device)}
						size="sm"
						variant="tertiary"
					>
						{pendingBench === device ? 'running…' : `run ${deviceLabel(device)}`}
					</Button>
				{/each}
			</div>
		</div>

		<div class="overflow-x-auto rounded-lg border">
			<table class="w-full min-w-[640px] text-left text-sm" data-testid="control-bench">
				<thead class="bg-muted/50 text-muted-foreground">
					<tr>
						<th class="px-4 py-2 font-medium">Device</th>

						<th class="px-4 py-2 font-medium">Model</th>

						<th class="px-4 py-2 text-right font-medium">TTFT</th>

						<th class="px-4 py-2 text-right font-medium">Prefill t/s</th>

						<th class="px-4 py-2 text-right font-medium">Decode t/s</th>
					</tr>
				</thead>

				<tbody class="divide-y">
					{#each benchResults as row (row.file)}
						<tr>
							<td class="px-4 py-2">
								{deviceLabel(row.device)}

								{#if row.device_id}
									<span class="text-xs text-muted-foreground">({row.device_id})</span>
								{/if}
							</td>

							<td
								class="max-w-[260px] truncate px-4 py-2 text-muted-foreground"
								title={row.model ?? ''}
							>
								{row.model ?? '—'}
							</td>

							<td class="px-4 py-2 text-right tabular-nums">
								{row.ttft_ms === null ? '—' : `${Math.round(row.ttft_ms)} ms`}
							</td>

							<td class="px-4 py-2 text-right tabular-nums">
								{row.prefill_tps === null ? '—' : row.prefill_tps.toFixed(1)}
							</td>

							<td class="px-4 py-2 text-right tabular-nums">
								{row.decode_tps === null ? '—' : row.decode_tps.toFixed(1)}
							</td>
						</tr>
					{:else}
						<tr>
							<td class="px-4 py-3 text-muted-foreground" colspan="5">
								No benchmark results yet — run one above.
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	</section>
</div>
