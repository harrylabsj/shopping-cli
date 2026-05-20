import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const OPENCLAW_PLUGIN_ID = 'shopping-plugin';

const MODULE_DIR = path.dirname(fileURLToPath(import.meta.url));
const DEFAULT_OPENCLAW_SKILL_ROOT = path.join(os.homedir(), '.openclaw', 'workspace', 'skills', 'shopping');
const DEFAULT_HERMES_SKILL_ROOT = path.join(os.homedir(), '.hermes', 'skills', 'commerce', 'shopping');

function nonEmptyString(value) {
  if (typeof value !== 'string') return undefined;
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : undefined;
}

function stringList(value) {
  if (Array.isArray(value)) {
    return value.map(String).map((item) => item.trim()).filter(Boolean).join(',');
  }
  return nonEmptyString(value);
}

export function resolveProjectRoot(projectRoot) {
  const explicit = nonEmptyString(projectRoot) || nonEmptyString(process.env.SHOPPING_ROOT);
  if (explicit) return explicit;

  for (const candidate of [DEFAULT_OPENCLAW_SKILL_ROOT, DEFAULT_HERMES_SKILL_ROOT, MODULE_DIR]) {
    if (fs.existsSync(path.join(candidate, 'scripts', 'shopping.py'))) return candidate;
  }

  return DEFAULT_OPENCLAW_SKILL_ROOT;
}

export function resolveShoppingPluginConfig(api, pluginId = OPENCLAW_PLUGIN_ID) {
  const nestedConfig = api?.config?.plugins?.entries?.[pluginId]?.config || {};
  const directConfig = api?.pluginConfig || {};
  const cfg = { ...directConfig, ...nestedConfig };

  return {
    projectRoot: resolveProjectRoot(cfg.projectRoot),
    dataPath: nonEmptyString(cfg.dbPath)
      || nonEmptyString(cfg.dataPath)
      || nonEmptyString(process.env.SHOPPING_DB)
      || nonEmptyString(process.env.SHOPPING_DATA),
  };
}

export function buildShoppingCommand({ subcommandArgs = [], dataPath, projectRoot } = {}) {
  const root = resolveProjectRoot(projectRoot);
  const command = ['python3', path.join(root, 'scripts', 'shopping.py')];
  if (dataPath) command.push('--db', String(dataPath));
  command.push(...subcommandArgs.map(String));
  return command;
}

export function runShoppingCli({ subcommandArgs = [], dataPath, projectRoot } = {}) {
  const command = buildShoppingCommand({ subcommandArgs, dataPath, projectRoot });
  const result = spawnSync(command[0], command.slice(1), { encoding: 'utf8' });
  const stdout = (result.stdout || '').trim();
  const stderr = (result.stderr || '').trim();

  if (result.error) {
    return { ok: false, errorType: 'spawn_error', error: String(result.error.message || result.error), command };
  }
  if (result.status !== 0) {
    return { ok: false, errorType: 'cli_exit', error: `shopping exited with code ${result.status}`, exitCode: result.status, stdout, stderr, command };
  }
  if (!stdout) return { ok: true, status: 'empty_output' };

  try {
    const payload = JSON.parse(stdout);
    if (payload && typeof payload === 'object' && !Array.isArray(payload)) return { ok: true, ...payload };
    return { ok: true, value: payload };
  } catch {
    return { ok: true, text: stdout };
  }
}

function addOptionalArg(args, flag, value) {
  const normalized = nonEmptyString(value);
  if (normalized) args.push(flag, normalized);
}

function addOptionalNumber(args, flag, value) {
  if (value !== undefined && value !== null && value !== '') args.push(flag, String(value));
}

function addOptionalTags(args, value) {
  const tags = stringList(value);
  if (tags) args.push('--tags', tags);
}

function addOptionalListArg(args, flag, value) {
  const items = stringList(value);
  if (items) args.push(flag, items);
}

function withPluginConfig(api, handler) {
  return async (input = {}) => handler(input || {}, resolveShoppingPluginConfig(api));
}

function registerTool(api, spec) {
  if (typeof api.registerTool === 'function') api.registerTool(spec);
}

function registerCommand(api, spec) {
  if (typeof api.registerCommand === 'function') api.registerCommand(spec);
}

function toolSpec(api, spec, handler) {
  const wrapped = withPluginConfig(api, handler);
  return {
    ...spec,
    async execute(_id, input = {}) {
      return wrapped(input);
    },
    handler: wrapped,
  };
}

function formatHelp(config) {
  const lines = [
    'shopping-cli Plugin is loaded.',
    'Tools: shopping_create_merchant, shopping_add_product, shopping_search_merchants, shopping_search_products, shopping_buyer_ask, shopping_buyer_summarize, shopping_record_intent, shopping_run_merchant_agent.',
    'Command: /shopping search <query> runs a local marketplace product search.',
  ];
  if (config.dataPath) lines.push(`dbPath: ${config.dataPath}`);
  return lines.join('\n');
}

export function registerOpenClawPlugin(api) {
  registerTool(api, toolSpec(api, {
    name: 'shopping_create_merchant',
    description: 'Create a local shopping-cli merchant profile and delivery rule.',
    parameters: {
      type: 'object',
      additionalProperties: false,
      properties: {
        id: { type: 'string' },
        name: { type: 'string' },
        city: { type: 'string' },
        service_area: { type: 'string' },
        contact: { type: 'string' },
        hours: { type: 'string' },
        delivery_fee: { type: 'number' },
        delivery_eta_minutes: { type: 'integer' },
        tags: { type: 'array', items: { type: 'string' } },
      },
      required: ['id', 'name'],
    },
  }, async (input, config) => {
    const args = ['merchant', 'create', '--id', input.id, '--name', input.name, '--format', 'json'];
    addOptionalArg(args, '--city', input.city);
    addOptionalArg(args, '--service-area', input.service_area || input.serviceArea);
    addOptionalArg(args, '--contact', input.contact);
    addOptionalArg(args, '--hours', input.hours);
    addOptionalNumber(args, '--delivery-fee', input.delivery_fee);
    addOptionalNumber(args, '--delivery-eta-minutes', input.delivery_eta_minutes);
    addOptionalTags(args, input.tags);
    return runShoppingCli({ subcommandArgs: args, dataPath: config.dataPath, projectRoot: config.projectRoot });
  }));

  registerTool(api, toolSpec(api, {
    name: 'shopping_add_product',
    description: 'Add a product listing to a local shopping-cli merchant catalog.',
    parameters: {
      type: 'object',
      additionalProperties: false,
      properties: {
        merchant: { type: 'string' },
        sku: { type: 'string' },
        title: { type: 'string' },
        price: { type: 'number' },
        stock: { type: 'integer' },
        currency: { type: 'string' },
        category: { type: 'string' },
        tags: { type: 'array', items: { type: 'string' } },
        description: { type: 'string' },
        delivery_attributes: { type: 'array', items: { type: 'string' } },
      },
      required: ['merchant', 'sku', 'title', 'price', 'stock'],
    },
  }, async (input, config) => {
    const args = [
      'product', 'add',
      '--merchant', input.merchant,
      '--sku', input.sku,
      '--title', input.title,
      '--price', String(input.price),
      '--stock', String(input.stock),
      '--format', 'json',
    ];
    addOptionalArg(args, '--currency', input.currency);
    addOptionalArg(args, '--category', input.category);
    addOptionalTags(args, input.tags);
    addOptionalArg(args, '--description', input.description);
    addOptionalListArg(args, '--delivery-attributes', input.delivery_attributes);
    return runShoppingCli({ subcommandArgs: args, dataPath: config.dataPath, projectRoot: config.projectRoot });
  }));

  registerTool(api, toolSpec(api, {
    name: 'shopping_search_merchants',
    description: 'Search local shopping-cli merchants by query and city.',
    parameters: {
      type: 'object',
      additionalProperties: false,
      properties: {
        query: { type: 'string' },
        city: { type: 'string' },
      },
    },
  }, async (input, config) => {
    const args = ['search', 'merchants', '--format', 'json'];
    addOptionalArg(args, '--query', input.query);
    addOptionalArg(args, '--city', input.city);
    return runShoppingCli({ subcommandArgs: args, dataPath: config.dataPath, projectRoot: config.projectRoot });
  }));

  registerTool(api, toolSpec(api, {
    name: 'shopping_search_products',
    description: 'Search local shopping-cli products with deterministic inventory and delivery data.',
    parameters: {
      type: 'object',
      additionalProperties: false,
      properties: {
        query: { type: 'string' },
        city: { type: 'string' },
        area: { type: 'string' },
        max_price: { type: 'number' },
        include_out_of_stock: { type: 'boolean' },
      },
    },
  }, async (input, config) => {
    const args = ['search', 'products', '--format', 'json'];
    addOptionalArg(args, '--query', input.query);
    addOptionalArg(args, '--city', input.city);
    addOptionalArg(args, '--area', input.area);
    addOptionalNumber(args, '--max-price', input.max_price);
    if (input.include_out_of_stock) args.push('--include-out-of-stock');
    return runShoppingCli({ subcommandArgs: args, dataPath: config.dataPath, projectRoot: config.projectRoot });
  }));

  registerTool(api, toolSpec(api, {
    name: 'shopping_buyer_ask',
    description: 'Search candidates and open a buyer consultation conversation.',
    parameters: {
      type: 'object',
      additionalProperties: false,
      properties: {
        buyer: { type: 'string' },
        text: { type: 'string' },
        city: { type: 'string' },
        area: { type: 'string' },
      },
      required: ['buyer', 'text'],
    },
  }, async (input, config) => {
    const args = ['buyer', 'ask', '--buyer', input.buyer, '--text', input.text, '--format', 'json'];
    addOptionalArg(args, '--city', input.city);
    addOptionalArg(args, '--area', input.area);
    return runShoppingCli({ subcommandArgs: args, dataPath: config.dataPath, projectRoot: config.projectRoot });
  }));

  registerTool(api, toolSpec(api, {
    name: 'shopping_buyer_summarize',
    description: 'Summarize a consultation option, warnings, and missing facts.',
    parameters: {
      type: 'object',
      additionalProperties: false,
      properties: {
        conversation: { type: 'string' },
      },
      required: ['conversation'],
    },
  }, async (input, config) => runShoppingCli({
    subcommandArgs: ['buyer', 'summarize', '--conversation', input.conversation, '--format', 'json'],
    dataPath: config.dataPath,
    projectRoot: config.projectRoot,
  })));

  registerTool(api, toolSpec(api, {
    name: 'shopping_record_intent',
    description: 'Record quote_request or purchase_intent as conversation context only.',
    parameters: {
      type: 'object',
      additionalProperties: false,
      properties: {
        conversation: { type: 'string' },
        intent: { type: 'string', enum: ['quote_request', 'purchase_intent'] },
        text: { type: 'string' },
      },
      required: ['conversation', 'intent', 'text'],
    },
  }, async (input, config) => runShoppingCli({
    subcommandArgs: ['buyer', 'intent', '--conversation', input.conversation, '--intent', input.intent, '--text', input.text, '--format', 'json'],
    dataPath: config.dataPath,
    projectRoot: config.projectRoot,
  })));

  registerTool(api, toolSpec(api, {
    name: 'shopping_run_merchant_agent',
    description: 'Run one deterministic resident merchant-agent polling pass.',
    parameters: {
      type: 'object',
      additionalProperties: false,
      properties: {
        merchant: { type: 'string' },
      },
      required: ['merchant'],
    },
  }, async (input, config) => runShoppingCli({
    subcommandArgs: ['agent', 'run', '--merchant', input.merchant, '--once', '--format', 'json'],
    dataPath: config.dataPath,
    projectRoot: config.projectRoot,
  })));

  registerCommand(api, {
    name: 'shopping',
    description: 'Show shopping-cli plugin help or run a local product search.',
    acceptsArgs: true,
    handler: async (ctx = {}) => {
      const rawArgs = String(ctx.args || ctx.text || '').trim();
      const config = resolveShoppingPluginConfig(api);
      if (rawArgs.startsWith('search ')) {
        const query = rawArgs.slice('search '.length).trim();
        const payload = runShoppingCli({
          subcommandArgs: ['search', 'products', '--query', query, '--format', 'json'],
          dataPath: config.dataPath,
          projectRoot: config.projectRoot,
        });
        return { text: JSON.stringify(payload, null, 2) };
      }
      return { text: formatHelp(config) };
    },
  });
}
