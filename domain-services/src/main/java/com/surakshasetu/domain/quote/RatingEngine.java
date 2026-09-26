package com.surakshasetu.domain.quote;

import com.surakshasetu.domain.contract.model.Gender;
import java.math.BigDecimal;
import java.util.Optional;
import org.jspecify.annotations.Nullable;

/**
 * The rating port: indicative premiums come only from here. Suitability uses it for the
 * affordability estimate and eligibility for the rating basis; Step 7's quote adapter prices quotes
 * through it.
 */
public interface RatingEngine {

  /**
   * The indicative annual premium for a product at this sum assured, or empty when it can't be
   * rated. {@code tobacco} null means undisclosed.
   */
  Optional<BigDecimal> annualPremium(
      String uin,
      int age,
      @Nullable Boolean tobacco,
      @Nullable Gender gender,
      BigDecimal sumAssured,
      int termYears);

  /** Whether the product's rate table differs by gender, so S1 must ask for it. */
  boolean ratesByGender(String uin);
}
