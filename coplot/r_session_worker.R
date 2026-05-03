args <- commandArgs(trailingOnly = TRUE)
plots_dir <- NULL
if (length(args) >= 2) {
  for (idx in seq_len(length(args) - 1)) {
    if (identical(args[[idx]], "--plots-dir")) {
      plots_dir <- args[[idx + 1]]
    }
  }
}

suppressPackageStartupMessages(library(jsonlite))
renv_activate <- file.path("coplot", "renv", "activate.R")
if (file.exists(renv_activate)) {
  invisible(capture.output(source(renv_activate, local = TRUE)))
}

`%||%` <- function(left, right) {
  if (is.null(left)) right else left
}

session_env <- new.env(parent = globalenv())
session_env$coplot_plots_dir <- plots_dir

execute_code <- function(code, interactive = FALSE) {
  ok <- TRUE
  stderr <- ""
  stdout <- capture.output({
    tryCatch(
      {
        exprs <- parse(text = code)
        if (interactive && length(exprs) == 1) {
          value <- eval(exprs[[1]], envir = session_env)
          if (!is.null(value)) {
            print(value)
          }
        } else {
          for (expr in exprs) {
            eval(expr, envir = session_env)
          }
        }
      },
      error = function(error) {
        ok <<- FALSE
        stderr <<- paste0(conditionMessage(error), "\n")
      }
    )
  })
  list(
    stdout = paste(stdout, collapse = "\n"),
    stderr = stderr,
    ok = ok,
    artifacts = list()
  )
}

stdin <- file("stdin", open = "r")
while (length(line <- readLines(stdin, n = 1, warn = FALSE)) == 1) {
  response <- tryCatch(
    {
      request <- jsonlite::fromJSON(line, simplifyVector = FALSE)
      execute_code(
        as.character(request$code %||% ""),
        interactive = isTRUE(request$interactive)
      )
    },
    error = function(error) {
      list(stdout = "", stderr = paste0(conditionMessage(error), "\n"), ok = FALSE, artifacts = list())
    }
  )
  cat(jsonlite::toJSON(response, auto_unbox = TRUE, null = "null"), "\n", sep = "")
  flush.console()
}
