package com.surakshasetu.domain.quote;

import com.surakshasetu.domain.catalog.CatalogRepository;
import com.surakshasetu.domain.common.BusinessDates;
import com.surakshasetu.domain.common.Problems;
import com.surakshasetu.domain.common.Uuid7;
import com.surakshasetu.domain.contract.model.Frequency;
import com.surakshasetu.domain.contract.model.PptOption;
import com.surakshasetu.domain.contract.model.PremiumQuote;
import com.surakshasetu.domain.contract.model.Product;
import com.surakshasetu.domain.contract.model.ProductStatus;
import com.surakshasetu.domain.contract.model.QuoteRequest;
import com.surakshasetu.domain.contract.model.Rider;
import com.surakshasetu.domain.quote.RatingEngine.Basis;
import java.math.BigDecimal;
import java.math.RoundingMode;
import java.time.LocalDate;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import org.jspecify.annotations.Nullable;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Component;

/**
 * The quote adapter in front of the rating engine (TDD §3.8). It checks a request against the
 * product's limits, prices it through {@link RatingEngine} and issues the indicative quote. Quotes,
 * ranking options and alternatives all come through here, so premiums have one source.
 */
@Component
public class QuoteAdapter {

  /** Cover and term within a product's limits for one age. */
  public record Sizing(BigDecimal sumAssured, int termYears) {}

  private final CatalogRepository catalog;
  private final RatingEngine rating;
  private final int validityDays;
  // ponytail: an in-memory counter per IST day and per instance that restarts at 1, so quote_id is
  // a readable reference and decision_id the unique key. Use a database sequence if quote ids must
  // be unique across instances and restarts.
  private LocalDate day = LocalDate.MIN;
  private int seq;

  QuoteAdapter(
      CatalogRepository catalog,
      RatingEngine rating,
      @Value("${surakshasetu.quote.validity-days}") int validityDays) {
    this.catalog = catalog;
    this.rating = rating;
    this.validityDays = validityDays;
  }

  /**
   * The product, if it is on sale today: in force, launched and inside its effective dates. 404 for
   * an unknown UIN; 409 PRODUCT_WITHDRAWN for one that is withdrawn, expired or not launched.
   */
  public Product onSale(String uin) {
    return catalog
        .product(uin, BusinessDates.on(null))
        .filter(p -> p.getStatus() == ProductStatus.IN_FORCE && p.getLaunchEnabled())
        .orElseThrow(
            () ->
                catalog.exists(uin)
                    ? Problems.problem(
                        HttpStatus.CONFLICT, "PRODUCT_WITHDRAWN", "the product is not on sale")
                    : Problems.notFound("no such product"));
  }

  /**
   * 422 TOBACCO_UNDISCLOSED, or 422 QUOTE_OUT_OF_BOUNDS for the first member outside the product's
   * limits: age, cover (range, then step), term, PPT, frequency, riders, then rider cover.
   */
  public static void validate(Product p, QuoteRequest r, BigDecimal step) {
    if (r.getTobacco12m() == null) {
      throw Problems.problem(
          HttpStatus.UNPROCESSABLE_CONTENT,
          "TOBACCO_UNDISCLOSED",
          "tobacco use was not disclosed, so premiums are withheld");
    }
    int age = r.getAgeYears();
    if (age < p.getEntryAgeMin() || age > ageMax(p)) {
      throw Problems.outOfBounds("age_years", p.getEntryAgeMin(), ageMax(p), null);
    }
    BigDecimal sa = new BigDecimal(r.getSumAssuredInr());
    BigDecimal saMin = new BigDecimal(p.getSaMinInr());
    BigDecimal saMax = p.getSaMaxInr() == null ? null : new BigDecimal(p.getSaMaxInr());
    if (sa.compareTo(saMin) < 0
        || (saMax != null && sa.compareTo(saMax) > 0)
        || sa.remainder(step).signum() != 0) {
      throw Problems.outOfBounds("sum_assured_inr", saMin, saMax, step);
    }
    int termMax = termMax(p, age);
    if (r.getTermYears() < p.getTermYearsMin() || r.getTermYears() > termMax) {
      throw Problems.outOfBounds("term_years", p.getTermYearsMin(), termMax, null);
    }
    if (!p.getPptOptions().contains(r.getPpt())) {
      throw Problems.outOfBounds("ppt", null, null, null);
    }
    if ((r.getPpt() == PptOption.SINGLE) != (r.getFrequency() == Frequency.SINGLE)) {
      throw Problems.outOfBounds("frequency", null, null, null);
    }
    Set<String> seen = new HashSet<>();
    for (String uin : r.getRiderUins()) {
      Rider rider = attachable(p, uin);
      if (rider == null || !seen.add(uin)) {
        throw Problems.outOfBounds("rider_uins", null, null, null);
      }
      if (rider.getSaMaxInr() != null && sa.compareTo(new BigDecimal(rider.getSaMaxInr())) > 0) {
        throw Problems.outOfBounds("rider_uins", null, new BigDecimal(rider.getSaMaxInr()), null);
      }
    }
  }

  /**
   * The cover and term nearest the wanted ones that {@link #validate} accepts for this age: cover
   * rounded up to the step and clamped to the product's range, term clamped to its range and
   * maturity age. Empty when the product doesn't take the age.
   */
  public static Optional<Sizing> size(
      Product p, int age, BigDecimal cover, int termYears, BigDecimal step) {
    BigDecimal low = up(new BigDecimal(p.getSaMinInr()), step);
    BigDecimal high = p.getSaMaxInr() == null ? null : down(new BigDecimal(p.getSaMaxInr()), step);
    if (age < p.getEntryAgeMin() || age > ageMax(p) || (high != null && low.compareTo(high) > 0)) {
      return Optional.empty();
    }
    BigDecimal sa = up(cover, step).max(low);
    if (high != null) {
      sa = sa.min(high);
    }
    return Optional.of(new Sizing(sa, Math.clamp(termYears, p.getTermYearsMin(), termMax(p, age))));
  }

  /** The rider, if the product lists it and it attaches to the product. */
  public static @Nullable Rider attachable(Product p, String riderUin) {
    return p.getRiders().stream()
        .filter(r -> r.getUin().equals(riderUin) && r.getAttachesTo().contains(p.getUin()))
        .findFirst()
        .orElse(null);
  }

  /**
   * The indicative quote for a validated request, or empty when the rating engine can't rate it.
   * {@code inputsSha256} is the hash of the request as sent, or as built by the service.
   */
  public Optional<PremiumQuote> price(Product p, QuoteRequest r, String inputsSha256) {
    BigDecimal sa = new BigDecimal(r.getSumAssuredInr());
    return rating
        .rate(
            new Basis(
                p.getUin(),
                r.getAgeYears(),
                r.getTobacco12m(),
                r.getGender(),
                sa,
                r.getTermYears(),
                r.getPpt(),
                r.getRiderUins()))
        .map(
            premium -> {
              LocalDate today = BusinessDates.on(null);
              Map<String, String> riders = new LinkedHashMap<>();
              premium.riders().forEach((uin, amount) -> riders.put(uin, money(amount)));
              return new PremiumQuote()
                  .decisionId(Uuid7.next())
                  .quoteId(nextQuoteId(today))
                  .uin(p.getUin())
                  .sumAssuredInr(money(sa))
                  .termYears(r.getTermYears())
                  .ppt(r.getPpt())
                  .annualPremiumInr(money(premium.total()))
                  .frequency(r.getFrequency())
                  .validUntil(today.plusDays(validityDays))
                  .indicative(true)
                  .riderPremiums(riders)
                  .gstIncluded(true)
                  .ratingVersion(rating.version())
                  .inputsSha256(inputsSha256)
                  .reasonCodes(List.of());
            });
  }

  /** A copy of a request, for building variants of it. */
  public static QuoteRequest copy(QuoteRequest r) {
    return new QuoteRequest(
            r.getPins(),
            r.getUin(),
            r.getSumAssuredInr(),
            r.getTermYears(),
            r.getPpt(),
            List.copyOf(r.getRiderUins()),
            r.getAgeYears(),
            r.getTobacco12m(),
            r.getFrequency())
        .gender(r.getGender());
  }

  /** Years the premium is paid: the term, 10 for limited_10 (or the term if shorter), 1 single. */
  public static int pptYears(PptOption ppt, int termYears) {
    return switch (ppt) {
      case REGULAR -> termYears;
      case LIMITED_10 -> Math.min(10, termYears);
      case SINGLE -> 1;
    };
  }

  /** Rupees to at most 2 decimals, without trailing zeros. */
  public static String money(BigDecimal amount) {
    return amount.setScale(2, RoundingMode.HALF_UP).stripTrailingZeros().toPlainString();
  }

  public static BigDecimal up(BigDecimal amount, BigDecimal step) {
    return amount.divide(step, 0, RoundingMode.CEILING).multiply(step);
  }

  public static BigDecimal down(BigDecimal amount, BigDecimal step) {
    return amount.divide(step, 0, RoundingMode.FLOOR).multiply(step);
  }

  /** The oldest entry age that still leaves the minimum term before the maturity age. */
  private static int ageMax(Product p) {
    return Math.min(p.getEntryAgeMax(), p.getMaturityAgeMax() - p.getTermYearsMin());
  }

  private static int termMax(Product p, int age) {
    return Math.min(p.getTermYearsMax(), p.getMaturityAgeMax() - age);
  }

  private synchronized String nextQuoteId(LocalDate today) {
    if (!today.equals(day)) {
      day = today;
      seq = 0;
    }
    return "Q-%s-%04d".formatted(today, ++seq);
  }
}
