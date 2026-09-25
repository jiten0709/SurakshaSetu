package com.surakshasetu.domain;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import javax.sql.DataSource;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.jdbc.core.simple.JdbcClient;

/** Health is UP with the database reached as domain_rw, never a superuser (least privilege). */
class HealthIT extends DomainApiTestSupport {

  @Autowired DataSource dataSource;

  @Test
  void actuatorHealthIsUpAsDomainRw() throws Exception {
    mvc.perform(get("/actuator/health"))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$.status").value("UP"));

    assertThat(
            JdbcClient.create(dataSource).sql("SELECT current_user").query(String.class).single())
        .isEqualTo("domain_rw");
  }
}
