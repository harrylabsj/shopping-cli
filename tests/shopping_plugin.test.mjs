import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  buildShoppingCommand,
  registerOpenClawPlugin,
  resolveShoppingPluginConfig,
} from '../plugins/shopping-plugin/openclaw_compat.js';

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

test('buildShoppingCommand points at the bundled Python CLI', () => {
  const command = buildShoppingCommand({
    projectRoot: '/tmp/shopping',
    dataPath: '/tmp/shopping.sqlite',
    subcommandArgs: ['search', 'products', '--format', 'json'],
  });

  assert.deepEqual(command, [
    'python3',
    path.join('/tmp/shopping', 'scripts', 'shopping.py'),
    '--db',
    '/tmp/shopping.sqlite',
    'search',
    'products',
    '--format',
    'json',
  ]);
});

test('resolveShoppingPluginConfig reads OpenClaw plugin config', () => {
  const api = {
    config: {
      plugins: {
        entries: {
          'shopping-plugin': {
            config: {
              projectRoot: '/tmp/project',
              dbPath: '/tmp/data.sqlite',
            },
          },
        },
      },
    },
  };

  assert.deepEqual(resolveShoppingPluginConfig(api), {
    projectRoot: '/tmp/project',
    dataPath: '/tmp/data.sqlite',
  });
});

test('registerOpenClawPlugin exposes marketplace tools and command', () => {
  const calls = {
    tools: [],
    commands: [],
  };
  const api = {
    registerTool(spec) {
      calls.tools.push(spec);
    },
    registerCommand(spec) {
      calls.commands.push(spec);
    },
    config: {},
  };

  registerOpenClawPlugin(api);

  assert.deepEqual(new Set(calls.tools.map((tool) => tool.name)), new Set([
    'shopping_create_merchant',
    'shopping_add_product',
    'shopping_search_merchants',
    'shopping_search_products',
    'shopping_buyer_ask',
    'shopping_buyer_summarize',
    'shopping_record_intent',
    'shopping_run_merchant_agent',
  ]));
  assert.equal(calls.commands.length, 1);
  assert.equal(calls.commands[0].name, 'shopping');
});

test('registered local tools can create and search products', async () => {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'shopping-openclaw-'));
  const dataPath = path.join(tmpDir, 'shopping.sqlite');
  const tools = new Map();
  const api = {
    registerTool(spec) {
      tools.set(spec.name, spec);
    },
    config: {
      plugins: {
        entries: {
          'shopping-plugin': {
            config: {
              projectRoot: REPO_ROOT,
              dataPath,
            },
          },
        },
      },
    },
  };

  registerOpenClawPlugin(api);

  const merchant = await tools.get('shopping_create_merchant').handler({
    id: 'seller-a',
    name: 'West Lake Tea',
    city: 'Hangzhou',
    service_area: 'West Lake',
    delivery_eta_minutes: 45,
    contact: 'wechat:westlake',
    tags: ['tea', 'gift'],
  });
  assert.equal(merchant.ok, true);

  const product = await tools.get('shopping_add_product').handler({
    merchant: 'seller-a',
    sku: 'tea-a',
    title: 'Longjing Gift Box',
    price: 88,
    stock: 5,
    category: 'tea',
    tags: ['longjing', 'gift'],
  });
  assert.equal(product.ok, true);

  const search = await tools.get('shopping_search_products').handler({
    query: 'longjing',
  });
  assert.equal(search.ok, true);
  assert.equal(search.results[0].sku, 'tea-a');

  const merchants = await tools.get('shopping_search_merchants').handler({
    query: 'west lake',
    city: 'Hangzhou',
  });
  assert.equal(merchants.ok, true);
  assert.equal(merchants.results[0].id, 'seller-a');

  const ask = await tools.get('shopping_buyer_ask').handler({
    buyer: 'alice',
    text: 'longjing gift delivery today',
    city: 'Hangzhou',
  });
  assert.equal(ask.ok, true);
  assert.equal(ask.conversation.id, 'CONV-0001');

  const agent = await tools.get('shopping_run_merchant_agent').handler({
    merchant: 'seller-a',
  });
  assert.equal(agent.ok, true);
  assert.equal(agent.replied[0].conversation_id, 'CONV-0001');
});

test('OpenClaw package metadata is present and versioned with package.json', () => {
  const pluginRoot = path.join(REPO_ROOT, 'plugins', 'shopping-plugin');
  const pkg = JSON.parse(fs.readFileSync(path.join(pluginRoot, 'package.json'), 'utf8'));
  const manifest = JSON.parse(fs.readFileSync(path.join(pluginRoot, 'openclaw.plugin.json'), 'utf8'));

  assert.equal(pkg.name, 'shopping-plugin');
  assert.equal(manifest.id, 'shopping-plugin');
  assert.equal(manifest.version, pkg.version);
  assert.ok(pkg.openclaw.extensions.includes('./index.js'));
});
