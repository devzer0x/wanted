// EIP-1193 (provider) and EIP-6963 (multi-provider discovery) surfaces, narrowed to what the
// wallet button actually calls. No wallet SDK is imported — this is the injected provider
// contract itself, called directly with viem's `custom` transport per the brief.

export interface EIP1193Provider {
  request: (args: { method: string; params?: unknown[] | Record<string, unknown> }) => Promise<unknown>;
  on?: (event: string, handler: (...args: unknown[]) => void) => void;
  removeListener?: (event: string, handler: (...args: unknown[]) => void) => void;
}

export interface EIP6963ProviderInfo {
  uuid: string;
  name: string;
  icon: string;
  rdns: string;
}

export interface EIP6963ProviderDetail {
  info: EIP6963ProviderInfo;
  provider: EIP1193Provider;
}

export interface EIP6963AnnounceProviderEvent extends Event {
  detail: EIP6963ProviderDetail;
}
