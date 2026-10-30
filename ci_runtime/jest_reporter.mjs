// Jest's pooled workers keep transformer setup warm. A completed-file event is
// emitted only after its coverage JSON has reached disk.
import { createHash } from 'node:crypto';
import { mkdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';

export default class DurableFileReporter {
  constructor(_config, options) {
    this.state = options.state;
    this.coverage = options.coverage;
  }

  onTestResult(test, result) {
    const filename = path.relative(process.cwd(), test.path).split(path.sep).join('/');
    const identifier = createHash('sha256').update(filename).digest('hex');
    const success = !result.testExecError && result.numFailingTests === 0
      && result.numPassingTests + result.numPendingTests + (result.numTodoTests || 0) > 0;
    if (this.coverage && success) {
      if (!result.coverage || typeof result.coverage !== 'object')
        throw new Error('Jest completed a file without coverage data');
      const directory = path.join(this.state, 'istanbul', identifier);
      mkdirSync(directory, {recursive: true});
      writeFileSync(path.join(directory, 'coverage-final.json'), JSON.stringify(result.coverage));
    }
    if (!success && result.failureMessage) process.stdout.write(result.failureMessage + '\n');
    const event = {filename, success,
      recoverable: success && result.numPendingTests === 0 && (result.numTodoTests || 0) === 0,
      seconds: Math.max(0, (result.perfStats.end - result.perfStats.start) / 1000)};
    process.stdout.write(`CI_RUNTIME_JEST_RESULT=${JSON.stringify(event)}\n`);
  }
}
