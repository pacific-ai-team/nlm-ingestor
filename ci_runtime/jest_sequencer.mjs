// Use the frozen measured file order with Jest's supported sequencer API.
// The pool assigns the next longest remaining file to each available worker.
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';

const require = createRequire(path.join(process.cwd(), 'package.json'));
const Sequencer = require('@jest/test-sequencer').default;

export default class MeasuredSequencer extends Sequencer {
  sort(tests) {
    const files = JSON.parse(readFileSync(process.env.CI_RUNTIME_JEST_ORDER, 'utf8'));
    const ranks = new Map(files.map((file, rank) => [path.resolve(file), rank]));
    if (ranks.size !== files.length || tests.length !== files.length
        || tests.some(test => !ranks.has(test.path))) {
      throw new Error('Jest collection differs from the frozen measured file order');
    }
    return [...tests].sort((left, right) => ranks.get(left.path) - ranks.get(right.path));
  }
}
