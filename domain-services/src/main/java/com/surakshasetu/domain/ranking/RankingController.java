package com.surakshasetu.domain.ranking;

import com.surakshasetu.domain.catalog.CatalogRepository;
import com.surakshasetu.domain.common.BusinessDates;
import com.surakshasetu.domain.common.Jcs;
import com.surakshasetu.domain.common.RawJson;
import com.surakshasetu.domain.common.Rules;
import com.surakshasetu.domain.common.Uuid7;
import com.surakshasetu.domain.contract.RankingApi;
import com.surakshasetu.domain.contract.model.ProductStatus;
import com.surakshasetu.domain.contract.model.RankingRequest;
import com.surakshasetu.domain.contract.model.RankingResult;
import com.surakshasetu.domain.contract.model.RecommendedOption;
import com.surakshasetu.domain.quote.QuoteAdapter;
import java.util.List;
import java.util.UUID;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.RestController;

/**
 * S3 ranking under the pinned rules version: the products on sale on the request's date (a kill
 * switch removes one at once), ranked by {@link Ranker}, each priced through the quote adapter.
 * Empty options answer NO_ELIGIBLE_OPTION, and the orchestrator escalates.
 */
@RestController
class RankingController implements RankingApi {

  private static final Logger log = LoggerFactory.getLogger(RankingController.class);

  private final Rules rules;
  private final Ranker ranker;
  private final CatalogRepository catalog;
  private final QuoteAdapter adapter;

  RankingController(Rules rules, Ranker ranker, CatalogRepository catalog, QuoteAdapter adapter) {
    this.rules = rules;
    this.ranker = ranker;
    this.catalog = catalog;
    this.adapter = adapter;
  }

  @Override
  public ResponseEntity<RankingResult> rankOptions(RankingRequest request) {
    long started = System.nanoTime();
    Rules.Release release = rules.release(request.getPins().getRules());
    Ranker.Weights weights = ranker.weights(release.rulesVersion());
    String inputsSha256 = Jcs.sha256Hex(RawJson.current());
    UUID decisionId = Uuid7.next();

    List<Ranker.Candidate> onSale =
        catalog
            .products(ProductStatus.IN_FORCE, null, true, BusinessDates.on(request.getAsOf()))
            .stream()
            .map(p -> new Ranker.Candidate(p, catalog.quoteDefaults(p)))
            .toList();
    Ranker.Ranked ranked =
        Ranker.rank(
            request,
            onSale,
            weights,
            release.params().saRoundingStepInr(),
            (product, quote) -> adapter.price(product, quote, Jcs.modelSha256(quote)));
    List<RecommendedOption> options = ranked.options();
    RankingResult result =
        new RankingResult(
            decisionId,
            options,
            weights.rankerVersion(),
            request.getSuitability().getInputsSha256(),
            inputsSha256,
            options.isEmpty() ? List.of("NO_ELIGIBLE_OPTION") : List.of());
    log.info(
        "ranking {} under {}: {} of {} candidates {} in {} ms",
        decisionId,
        weights.rankerVersion(),
        options.size(),
        ranked.candidates(),
        options.stream().map(RecommendedOption::getUin).toList(),
        (System.nanoTime() - started) / 1_000_000);
    return ResponseEntity.ok(result);
  }
}
