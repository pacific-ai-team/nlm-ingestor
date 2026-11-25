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
      // Source-inspection suites can pass without importing any instrumented
      // module. Their contribution is legitimately empty; the final product
      // inventory still includes every uncovered source file.
      const directory = path.join(this.state, 'istanbul', identifier);
      mkdirSync(directory, {recursive: true});
      writeFileSync(path.join(directory, 'coverage-final.json'), JSON.stringify(result.coverage || {}));
    }
    if (!success && result.failureMessage) process.stdout.write(result.failureMessage + '\n');
    const event = {filename, success,
      recoverable: success && result.numPendingTests === 0 && (result.numTodoTests || 0) === 0,
      seconds: Number.isFinite(result.perfStats.end - result.perfStats.start)
        ? Math.max(0, (result.perfStats.end - result.perfStats.start) / 1000) : 0};
    process.stdout.write(`CI_RUNTIME_JEST_RESULT=${JSON.stringify(event)}\n`);
  }
  onRunComplete(_contexts, result) {
    writeFileSync(path.join(this.state, 'jest-summary.json'), JSON.stringify({
      tests: result.numTotalTests, failed: result.numFailedTests,
      passed: result.numPassedTests, pending: result.numPendingTests,
      failedFiles: result.numFailedTestSuites, interrupted: result.wasInterrupted,
      files: result.numTotalTestSuites,
    }));
  }

}
