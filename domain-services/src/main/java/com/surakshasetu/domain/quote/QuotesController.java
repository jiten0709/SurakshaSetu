package com.surakshasetu.domain.quote;

import static com.surakshasetu.domain.quote.QuoteAdapter.money;

import com.surakshasetu.domain.common.Jcs;
import com.surakshasetu.domain.common.Problems;
import com.surakshasetu.domain.common.RawJson;
import com.surakshasetu.domain.common.Rules;
import com.surakshasetu.domain.contract.QuotesApi;
import com.surakshasetu.domain.contract.model.Frequency;
import com.surakshasetu.domain.contract.model.PptOption;
import com.surakshasetu.domain.contract.model.PremiumQuote;
import com.surakshasetu.domain.contract.model.Product;
import com.surakshasetu.domain.contract.model.QuoteAlternative;
import com.surakshasetu.domain.contract.model.QuoteAlternative.ChangeEnum;
import com.surakshasetu.domain.contract.model.QuoteAlternativesRequest;
import com.surakshasetu.domain.contract.model.QuoteRequest;
import java.math.BigDecimal;
import java.util.ArrayList;
import java.util.List;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.RestController;

/**
 * Indicative quotes and "make it cheaper" alternatives (TDD §3.8). The pinned rules version gives
 * the cover step; every premium comes from {@link QuoteAdapter}.
 */
@RestController
class QuotesController implements QuotesApi {

  private static final Logger log = LoggerFactory.getLogger(QuotesController.class);
  private static final BigDecimal[] LOWER_COVER = {new BigDecimal("0.75"), new BigDecimal("0.50")};

  private final Rules rules;
  private final QuoteAdapter adapter;

  QuotesController(Rules rules, QuoteAdapter adapter) {
    this.rules = rules;
    this.adapter = adapter;
  }

  /** A variant of the customer's quote request, and what changed. */
  private record Variant(QuoteRequest request, ChangeEnum change) {}

  @Override
  public ResponseEntity<PremiumQuote> createQuote(QuoteRequest request) {
    long started = System.nanoTime();
    BigDecimal step = rules.release(request.getPins().getRules()).params().saRoundingStepInr();
    Product product = adapter.onSale(request.getUin());
    QuoteAdapter.validate(product, request, step);
    PremiumQuote quote =
        adapter
            .price(product, request, Jcs.sha256Hex(RawJson.current()))
            .orElseThrow(
                () -> {
                  log.warn("rating unavailable for {}", product.getUin());
                  return Problems.problem(
                      HttpStatus.SERVICE_UNAVAILABLE,
                      "SERVICE_UNAVAILABLE",
                      "the rating engine can't rate this request");
                });
    log.info(
        "quote {} {} for {} under {} in {} ms",
        quote.getDecisionId(),
        quote.getQuoteId(),
        product.getUin(),
        quote.getRatingVersion(),
        (System.nanoTime() - started) / 1_000_000);
    return ResponseEntity.ok(quote);
  }

  /**
   * Lower cover (−25% and −50%, down to the step, not below the minimum), fewer riders (each one
   * dropped, and none when there are several) and the other PPTs, in that order. Each carries its
   * protection gap against the recommended cover; a variant the engine can't rate is left out.
   */
  @Override
  public ResponseEntity<List<QuoteAlternative>> createQuoteAlternatives(
      QuoteAlternativesRequest body) {
    long started = System.nanoTime();
    BigDecimal step = rules.release(body.getPins().getRules()).params().saRoundingStepInr();
    QuoteRequest base =
        new QuoteRequest(
                body.getPins(),
                body.getUin(),
                body.getSumAssuredInr(),
                body.getTermYears(),
                body.getPpt(),
                body.getRiderUins(),
                body.getAgeYears(),
                body.getTobacco12m(),
                body.getFrequency())
            .gender(body.getGender());
    Product product = adapter.onSale(base.getUin());
    QuoteAdapter.validate(product, base, step);
    BigDecimal recommended = new BigDecimal(body.getRecommendedCoverInr());

    List<QuoteAlternative> alternatives = new ArrayList<>();
    for (Variant variant : variants(product, base, step)) {
      QuoteRequest request = variant.request();
      BigDecimal gap =
          recommended.subtract(new BigDecimal(request.getSumAssuredInr())).max(BigDecimal.ZERO);
      adapter
          .price(product, request, Jcs.modelSha256(request))
          .ifPresent(q -> alternatives.add(new QuoteAlternative(q, money(gap), variant.change())));
    }
    log.info(
        "quote alternatives for {}: {} in {} ms",
        product.getUin(),
        alternatives.size(),
        (System.nanoTime() - started) / 1_000_000);
    return ResponseEntity.ok(alternatives);
  }

  /** Variants built inside the limits the base request already passed. */
  private static List<Variant> variants(Product product, QuoteRequest base, BigDecimal step) {
    List<Variant> variants = new ArrayList<>();
    BigDecimal sa = new BigDecimal(base.getSumAssuredInr());
    BigDecimal floor = QuoteAdapter.up(new BigDecimal(product.getSaMinInr()), step);
    BigDecimal previous = sa;
    for (BigDecimal share : LOWER_COVER) {
      BigDecimal lower = QuoteAdapter.down(sa.multiply(share), step).max(floor);
      if (lower.compareTo(previous) < 0) {
        variants.add(
            new Variant(
                QuoteAdapter.copy(base).sumAssuredInr(money(lower)), ChangeEnum.LOWER_COVER));
        previous = lower;
      }
    }
    List<String> riders = base.getRiderUins();
    for (String dropped : riders) {
      List<String> fewer = riders.stream().filter(r -> !r.equals(dropped)).toList();
      variants.add(new Variant(QuoteAdapter.copy(base).riderUins(fewer), ChangeEnum.FEWER_RIDERS));
    }
    if (riders.size() > 1) {
      variants.add(
          new Variant(QuoteAdapter.copy(base).riderUins(List.of()), ChangeEnum.FEWER_RIDERS));
    }
    for (PptOption ppt : product.getPptOptions()) {
      if (ppt != base.getPpt()) {
        Frequency frequency =
            ppt == PptOption.SINGLE
                ? Frequency.SINGLE
                : base.getFrequency() == Frequency.SINGLE ? Frequency.ANNUAL : base.getFrequency();
        variants.add(
            new Variant(
                QuoteAdapter.copy(base).ppt(ppt).frequency(frequency), ChangeEnum.OTHER_PPT));
      }
    }
    return variants;
  }
}
