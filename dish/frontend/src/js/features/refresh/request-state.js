export class LocalBoardRequestState {
  constructor() {
    this.bootstrapSequence = 0; this.acceptedBoardGeneration = 0;
    this.continuationSequence = 0; this.detailSequence = 0; this.inFlightBySection = new Map();
  }
  beginBootstrap() { this.bootstrapSequence += 1; this.inFlightBySection.clear(); return this.bootstrapSequence; }
  isCurrentBootstrap(generation) { return generation === this.bootstrapSequence; }
  acceptBootstrap(generation) {
    if (!this.isCurrentBootstrap(generation)) return false;
    this.acceptedBoardGeneration = generation; this.inFlightBySection.clear(); return true;
  }
  beginContinuation(section) {
    if (!section?.nextCursor || section.loadMoreBlocked || this.acceptedBoardGeneration === 0
      || this.acceptedBoardGeneration !== this.bootstrapSequence || this.inFlightBySection.has(section.id)) return null;
    const request = Object.freeze({
      requestId: ++this.continuationSequence, boardGeneration: this.acceptedBoardGeneration,
      sectionId: section.id, continuityId: section.continuityId, cursor: section.nextCursor,
    });
    this.inFlightBySection.set(section.id, request); return request;
  }
  currentContinuationSection(request, board) {
    if (!request || this.bootstrapSequence !== request.boardGeneration || this.acceptedBoardGeneration !== request.boardGeneration) return null;
    if (this.inFlightBySection.get(request.sectionId) !== request) return null;
    const section = board?.sections.find((item) => item.id === request.sectionId);
    if (!section || section.continuityId !== request.continuityId || section.nextCursor !== request.cursor) return null;
    return section;
  }
  finishContinuation(request) {
    if (request && this.inFlightBySection.get(request.sectionId) === request) this.inFlightBySection.delete(request.sectionId);
  }
  beginDetail(taskId) { return Object.freeze({ sequence: ++this.detailSequence, taskId }); }
  isCurrentDetail(request) { return request?.sequence === this.detailSequence; }
  cancelDetail() { this.detailSequence += 1; }
  cancelAll() { this.beginBootstrap(); this.cancelDetail(); this.inFlightBySection.clear(); }
}
