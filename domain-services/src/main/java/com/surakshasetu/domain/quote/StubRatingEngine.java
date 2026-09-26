package com.surakshasetu.domain.quote;

import com.surakshasetu.domain.contract.model.Gender;
import java.math.BigDecimal;
import java.math.RoundingMode;
import java.util.Optional;
import org.jspecify.annotations.Nullable;
import org.springframework.stereotype.Component;

/**
 * DUMMY flat rates per ₹1,000 sum assured, so affordability can be exercised end to end before Step
 * 7 replaces this with the rate table. Undisclosed tobacco is priced as tobacco: an estimate for
 * affordability errs high. No gender rating, so gender is not asked yet.
 */
@Component
class StubRatingEngine implements RatingEngine {

  private static final BigDecimal NON_TOBACCO_PER_MILLE = new BigDecimal("1.50");
  private static final BigDecimal TOBACCO_PER_MILLE = new BigDecimal("3.00");
  private static final BigDecimal MILLE = new BigDecimal("1000");

  @Override
  public Optional<BigDecimal> annualPremium(
      String uin,
      int age,
      @Nullable Boolean tobacco,
      @Nullable Gender gender,
      BigDecimal sumAssured,
      int termYears) {
    BigDecimal rate = Boolean.FALSE.equals(tobacco) ? NON_TOBACCO_PER_MILLE : TOBACCO_PER_MILLE;
    return Optional.of(sumAssured.multiply(rate).divide(MILLE, 2, RoundingMode.HALF_UP));
  }

  @Override
  public boolean ratesByGender(String uin) {
    return false;
  }
}
