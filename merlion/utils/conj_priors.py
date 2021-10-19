#
# Copyright (c) 2021 salesforce.com, inc.
# All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# For full license text, see the LICENSE file in the repo root or https://opensource.org/licenses/BSD-3-Clause
#
"""
Implementations of Bayesian conjugate priors & their online update rules.

.. autosummary::
    ConjPrior
    BetaBernoulli
    NormInvGamma
    MVNormInvWishart
    BayesianLinReg
    BayesianMVLinReg
"""
from abc import ABC, abstractmethod
import copy

import numpy as np
from scipy.special import loggamma
from scipy.linalg import pinv, pinvh
from scipy.stats import (
    bernoulli,
    beta,
    invgamma,
    invwishart,
    norm,
    multivariate_normal as mvnorm,
    t as student_t,
    multivariate_t as mvt,
)

from merlion.utils import TimeSeries


class ConjPrior(ABC):
    """
    Abstract base class for a Bayesian conjugate prior.
    Can be used with either `TimeSeries` or ``numpy`` arrays directly.
    """

    def __init__(self):
        self.n = 0
        self.dim = None
        self.t0 = None
        self.dt = None

    def to_dict(self):
        return {k: v.tolist() if hasattr(v, "tolist") else copy.deepcopy(v) for k, v in self.__dict__.items()}

    @classmethod
    def from_dict(cls, state_dict):
        ret = cls()
        for k, v in state_dict.items():
            setattr(ret, k, np.asarray(v))
        return ret

    def __copy__(self):
        ret = self.__class__()
        for k, v in self.__dict__.items():
            setattr(ret, k, copy.deepcopy(v))
        return ret

    def __deepcopy__(self, memodict={}):
        return self.__copy__()

    def process_time_series(self, x):
        """
        :return: ``(t, x)``, where ``t`` is a normalized list of timestamps, and ``x`` is a ``numpy`` array
            representing the input
        :rtype: ``Tuple[numpy.ndarray, numpy.ndarray]``
        """
        if x is None:
            return None, None

        # Initialize t0 and dt if needed
        if self.t0 is None or self.dt is None:
            if isinstance(x, TimeSeries):
                t0, tf = x.t0, x.tf
                self.t0 = t0
                self.dt = tf - t0 if tf != t0 else 1
            else:
                x = np.asarray(x)
                self.t0 = 0
                self.dt = 1 if x.ndim < 1 else len(x)

        # Convert time series to numpy, or convert numpy array to pseudo time series
        if isinstance(x, TimeSeries):
            t = (x.np_time_stamps - self.t0) / self.dt
            x = x.align().to_pd().values
        else:
            x = np.asarray(x)
            x = x.reshape((1, 1) if x.ndim < 1 else (len(x), -1))
            t = (np.arange(self.n, self.n + len(x)) - self.t0) / self.dt

        if self.dim is None:
            self.dim = x.shape[-1]
        else:
            assert x.shape[-1] == self.dim, f"Expected input with dimension {self.dim} but got {x.shape[-1]}"

        return t, x

    @staticmethod
    def _process_return(x, rv, return_rv, log):
        if x is None or return_rv:
            return rv
        try:
            return rv.logpdf(x) if log else rv.pdf(x)
        except AttributeError:
            return rv.logpmf(x) if log else rv.pmf(x)

    @abstractmethod
    def posterior(self, x, return_rv=False, log=True):
        """
        Predictive posterior (log) PDF for new observations, or the ``scipy.stats`` random variable where applicable.

        :param x: value(s) to evaluate posterior at (``None`` implies that we want to return the random variable)
        :param return_rv: whether to return the random variable directly
        :param log: whether to return the log PDF (instead of the PDF)
        """
        raise NotImplementedError

    @abstractmethod
    def update(self, x):
        """
        Update the conjugate prior based on new observations x.
        """
        raise NotImplementedError


class ScalarConjPrior(ConjPrior, ABC):
    """
    Abstract base class for a Bayesian conjugate prior for a scalar random variable.
    """

    def __init__(self):
        super().__init__()
        self.dim = 1

    def process_time_series(self, x):
        t, x = super().process_time_series(x)
        x = x.flatten() if x is not None else x
        return t, x


class BetaBernoulli(ScalarConjPrior):
    r"""
    Beta-Bernoulli conjugate prior for binary data. We assume the model

    .. math::

        \begin{align*}
        X &\sim \mathrm{Bernoulli}(\theta) \\
        \theta &\sim \mathrm{Beta}(\alpha, \beta)
        \end{align*}

    The update rule for data :math:`x_1, \ldots, x_n` is

    .. math::
        \begin{align*}
        \alpha &= \alpha + \sum_{i=1}^{n} \mathbb{I}[x_i = 1] \\
        \beta &= \beta + \sum_{i=1}^{n} \mathbb{I}[x_i = 0]
        \end{align*}

    """

    def __init__(self, sample=None):
        super().__init__()
        self.alpha = 1
        self.beta = 1
        if sample is not None:
            self.update(sample)

    def posterior(self, x, return_rv=False, log=True):
        r"""
        The posterior distribution of x is :math:`\mathrm{Bernoulli}(\alpha / (\alpha + \beta))`.
        """
        t, x = self.process_time_series(x)
        rv = bernoulli(self.alpha / (self.alpha + self.beta))
        return self._process_return(x=x, rv=rv, return_rv=return_rv, log=log)

    def theta_posterior(self, theta, return_rv=False, log=True):
        r"""
        The posterior distribution of :math:`\theta` is :math:`\mathrm{Beta}(\alpha, \beta)`.
        """
        rv = beta(self.alpha, self.beta)
        return self._process_return(x=theta, rv=rv, return_rv=return_rv, log=log)

    def update(self, x):
        t, x = self.process_time_series(x)
        self.n += len(x)
        self.alpha += x.sum()
        self.beta += (1 - x).sum()


class NormInvGamma(ScalarConjPrior):
    r"""
    Normal-InverseGamma conjugate prior. Following
    `Wikipedia <https://en.wikipedia.org/wiki/Normal-inverse-gamma_distribution>`__ and
    `Murphy (2007) <https://www.cs.ubc.ca/~murphyk/Papers/bayesGauss.pdf>`__, we assume the model

    .. math::

        \begin{align*}
        X &\sim \mathcal{N}(\mu, \sigma^2) \\
        \mu &\sim \mathcal{N}(\mu_0, \sigma^2 / n) \\
        \sigma^2 &\sim \mathrm{InvGamma}(\alpha, \beta)
        \end{align*}

    The update rule for data :math:`x_1, \ldots, x_n` is

    .. math::
        \begin{align*}
        \bar{x} &= \frac{1}{n} \sum_{i = 1}^{n} x_i \\
        \alpha &= \alpha + n/2 \\
        \beta &= \beta + \frac{1}{2} \sum_{i = 1}^{n} (x_i - \bar{x})^2 + \frac{1}{2} (\mu_0 - \bar{x})^2 \\
        \mu_0 &= \frac{n_0}{n_0 + n} \mu_0 + \frac{n}{n_0 + n} \bar{x} \\
        n_0 &= n_0 + n
        \end{align*}

    """

    def __init__(self, sample=None):
        super().__init__()
        self.mu_0 = 0
        self.alpha = 0
        self.beta = 0
        if sample is not None:
            self.update(sample)

    def update(self, x):
        t, x = self.process_time_series(x)
        n0, n = self.n, len(x)
        self.alpha = self.alpha + n / 2
        self.n = n0 + n

        xbar = np.mean(x)
        sample_comp = np.sum((x - xbar) ** 2)
        prior_comp = n0 * n / (n0 + n) * (self.mu_0 - xbar) ** 2
        self.beta = self.beta + sample_comp / 2 + prior_comp / 2
        self.mu_0 = self.mu_0 * n0 / (n0 + n) + xbar * n / (n0 + n)

    def mu_posterior(self, mu, return_rv=False, log=True):
        r"""
        The posterior for :math:`\mu` is :math:`\text{Student-t}_{2\alpha}(\mu_0, \beta / (n \alpha))`
        """
        scale = self.beta / (self.alpha * self.n)
        rv = student_t(loc=self.mu_0, scale=np.sqrt(scale), df=2 * self.alpha)
        return self._process_return(x=mu, rv=rv, return_rv=return_rv, log=log)

    def sigma2_posterior(self, sigma2, return_rv=False, log=True):
        r"""
        The posterior for :math:`\sigma^2` is :math:`\text{InvGamma}(\alpha, \beta)`.
        """
        rv = invgamma(a=self.alpha, scale=self.beta)
        return self._process_return(x=sigma2, rv=rv, return_rv=return_rv, log=log)

    def posterior(self, x, log=True, return_rv=False):
        r"""
        The posterior for :math:`x` is :math:`\text{Student-t}_{2\alpha}(\mu_0, (n+1) \beta / (n \alpha))`
        """
        t, x = self.process_time_series(x)
        scale = (self.beta * (self.n + 1)) / (self.alpha * self.n)
        rv = student_t(loc=self.mu_0, scale=np.sqrt(scale), df=2 * self.alpha)
        return self._process_return(x=x, rv=rv, return_rv=return_rv, log=log)


class MVNormInvWishart(ConjPrior):
    r"""
    Multivariate Normal-InverseWishart conjugate prior. Multivariate equivalent of Normal-InverseGamma.
    Following `Murphy (2007) <https://www.cs.ubc.ca/~murphyk/Papers/bayesGauss.pdf>`__, we assume the model

    .. math::

        \begin{align*}
        X &\sim \mathcal{N}_d(\mu, \Sigma) \\
        \mu &\sim \mathcal{N}_d(\mu_0, \Sigma / n) \\
        \Sigma &\sim \mathrm{InvWishart}_{\nu}(\Lambda)
        \end{align*}

    The update rule for data :math:`x_1, \ldots, x_n` is

    .. math::
        \begin{align*}
        \bar{x} &= \frac{1}{n} \sum_{i = 1}^{n} x_i \\
        \nu &= \nu + n/2 \\
        \Lambda &= \Lambda + \frac{n_0 n}{n_0 + n} (\mu_0 - \bar{x}) (\mu_0 - \bar{x})^T +
        \sum_{i = 1}^{n} (x_i - \bar{x}) (x_i - \bar{x})^T \\
        \mu_0 &= \frac{n_0}{n_0 + n} \mu_0 + \frac{n}{n_0 + n} \bar{x} \\
        n_0 &= n_0 + n
        \end{align*}
    """

    def __init__(self, sample=None):
        super().__init__()
        self.nu = 0
        self.mu_0 = None
        self.Lambda = None
        if sample is not None:
            self.update(sample)

    def update(self, x):
        t, x = self.process_time_series(x)

        n0 = self.n
        n, d = x.shape
        self.nu = self.nu + n

        sample_mean = np.mean(x, axis=0)
        sample_cov = np.cov(x, rowvar=False)
        if self.n == 0:
            self.mu_0 = sample_mean
            self.Lambda = sample_cov * n
            self.n = n

        else:
            delta = sample_mean - self.mu_0
            sample_comp = sample_cov * n
            prior_comp = (delta.T @ delta) * n * n0 / (n + n0)
            self.Lambda = self.Lambda + sample_comp + prior_comp
            self.mu_0 = self.mu_0 * n0 / (n0 + n) + sample_mean * n / (n0 + n)
            self.n = n0 + n

    def mu_posterior(self, mu, return_rv=False, log=True):
        r"""
        The posterior for :math:`\mu` is :math:`\text{Student-t}_{\nu-d+1}(\mu_0, \Lambda / (n (\nu - d + 1)))`
        """
        dof = max(self.nu - self.dim + 1, 1)
        shape = self.Lambda / (self.n * dof)
        rv = mvt(shape=shape, loc=self.mu_0, df=dof)
        return self._process_return(x=mu, rv=rv, return_rv=return_rv, log=log)

    def Sigma_posterior(self, sigma2, return_rv=False, log=True):
        r"""
        The posterior for :math:`\Sigma` is :math:`\text{InvWishart}_{\nu}(\Lambda^{-1})`
        """
        rv = invwishart(df=self.nu, scale=self.Lambda)
        return self._process_return(x=sigma2, rv=rv, return_rv=return_rv, log=log)

    def posterior(self, x, return_rv=False, log=True):
        r"""
        The posterior for :math:`x` is :math:`\text{Student-t}_{\nu-d+1}(\mu_0, (n + 1) \Lambda / (n (\nu - d + 1)))`
        """
        t, x = self.process_time_series(x)
        dof = max(self.nu - self.dim + 1, 1)
        shape = self.Lambda * (self.n + 1) / (self.n * dof)
        rv = mvt(shape=shape, loc=self.mu_0, df=dof)
        return self._process_return(x=x, rv=rv, return_rv=return_rv, log=log)


class BayesianLinReg(ConjPrior):
    r"""
    Bayesian Ordinary Linear Regression conjugate prior, which models a univariate input as a function of time.
    Following `Wikipedia <https://en.wikipedia.org/wiki/Bayesian_linear_regression>`__, we assume the model

    .. math::

        \begin{align*}
        x(t) &\sim \mathcal{N}(m t + b, \sigma^2) \\
        w &\sim \mathcal{N}((m_0, b_0), \sigma^2 \Lambda_0^{-1}) \\
        \sigma^2 &\sim \mathrm{InvGamma}(\alpha, \beta)
        \end{align*}

    Consider new data :math:`(t_1, x_1), \ldots, (t_n, x_n)`. Let :math:`T \in \mathbb{R}^{n \times 2}` be
    the matrix obtained by stacking the row vector of times with an all-ones row vector. Let
    :math:`w = (m, b) \in \mathbb{R}^{2}` be the full weight vector. Let :math:`x \in \mathbb{R}^{n}` denote
    all observed values. Then we have the update rule

    .. math::

        \begin{align*}
        w_{OLS} &= (T^T T)^{-1} T^T x \\
        \Lambda_n &= \Lambda_0 + T^T T \\
        w_n &= (\Lambda_0 + T^T T)^{-1} (\Lambda_0 w_0 + T^T T w_{OLS}) \\
        \alpha_n &= \alpha_0 + n / 2 \\
        \beta_n &= \beta_0 + \frac{1}{2}(x^T x + w_0^T \Lambda_0 w_0 - w_n^T \Lambda_n w_n)
        \end{align*}
    """

    def __init__(self, sample=None):
        super().__init__()
        self.w_0 = np.zeros(2)
        self.Lambda_0 = np.zeros((2, 2))
        self.alpha = 0
        self.beta = 0
        if sample is not None:
            self.update(sample)

    def update(self, x):
        t, x = self.process_time_series(x)
        t_full = np.stack((t, np.ones_like(t)), axis=-1)  # [t, 2]

        # Initial prediction
        self.w_0 = self.w_0.reshape((2, 1))
        pred0 = self.w_0.T @ self.Lambda_0 @ self.w_0

        # Update predictive coefficients & uncertainty
        design = t_full.T @ t_full
        ols = pinv(t_full) @ x
        self.w_0 = pinvh(self.Lambda_0 + design) @ (self.Lambda_0 @ self.w_0 + design @ ols)
        self.Lambda_0 = self.Lambda_0 + design

        # Updated prediction
        pred = self.w_0.T @ self.Lambda_0 @ self.w_0
        self.w_0 = self.w_0.flatten()

        # Update accumulators
        self.n = self.n + len(x)
        self.alpha = self.alpha + len(x) / 2
        self.beta = self.beta + (x.T @ x + pred0 - pred) / 2

    def posterior(self, x, return_rv=False, log=True, return_updated=False):
        r"""
        Let :math:`\Lambda_n, \alpha_n, \beta_n` be the posterior values obtained by updating
        the model on data :math:`(t_1, x_1), \ldots, (t_n, x_n)`. The predictive posterior has PDF

        .. math::

            \begin{align*}
            P((t, x)) &= \frac{1}{(2 \pi)^{-n/2}} \sqrt{\frac{\det \Lambda_n}{\det \Lambda_0}}
            \frac{\beta_0^{\alpha_0}}{\beta_n^{\alpha_n}}\frac{\Gamma(\alpha_n)}{\Gamma(\alpha_0)}
            \end{align*}
        """
        if x is None or return_rv:
            raise ValueError(
                "Bayesian linear regression doesn't have a scipy.stats random variable posterior. "
                "Please specify a non-``None`` value of ``x`` and set ``return_rv = False``."
            )
        t, x_np = self.process_time_series(x)
        updated = copy.deepcopy(self)
        updated.update(x)
        a = -len(x_np) / 2 * np.log(2 * np.pi)
        b = (np.linalg.slogdet(self.Lambda_0)[1] - np.linalg.slogdet(updated.Lambda_0)[1]) / 2
        c = self.alpha * np.log(self.beta) - updated.alpha * np.log(updated.beta)
        d = loggamma(updated.alpha) - loggamma(self.alpha)
        ret = (a + b + c + d if log else np.exp(a + b + c + d)).reshape(len(x_np))
        return (ret, updated) if return_updated else ret

    def naive_posterior(self, x, log=True):
        r"""
        Naive computation of the posterior using Bayes Rule, i.e.

        .. math::

            \hat{\sigma}^2 &= \mathbb{E}[\sigma^2] \\
            \hat{w} &= \mathbb{E}[w \mid \sigma^2 = \hat{\sigma}^2] \\
            p(x \mid t) &= \frac{
            p(w = \hat{w}, \sigma^2 = \hat{\sigma}^2)
            p(x \mid t, w = \hat{w}, \sigma^2 = \hat{\sigma}^2)}{
            p(w = \hat{w}, \sigma^2 = \hat{\sigma}^2 \mid x, t)}

        """
        t, x_np = self.process_time_series(x)

        # Get priors & MAP estimates for sigma^2 and w; get the MAP estimate for x(t)
        prior_sigma2 = invgamma(a=self.alpha, scale=self.beta)
        sigma2_hat = prior_sigma2.mean()
        prior_w = mvnorm(self.w_0, sigma2_hat * pinvh(self.Lambda_0))
        w_hat = self.w_0
        xhat = np.stack((t, np.ones_like(t)), axis=-1) @ w_hat

        # Get posteriors
        updated = copy.deepcopy(self)
        updated.update(x)
        post_sigma2 = invgamma(a=updated.alpha, scale=updated.beta)
        post_w = mvnorm(updated.w_0, sigma2_hat * pinvh(updated.Lambda_0))

        # Apply Bayes' rule
        evidence = norm(xhat, np.sqrt(sigma2_hat)).logpdf(x_np).reshape(len(x_np))
        prior = prior_sigma2.logpdf(sigma2_hat) + prior_w.logpdf(w_hat)
        post = post_sigma2.logpdf(sigma2_hat) + post_w.logpdf(w_hat)
        logp = evidence + prior.item() - post.item()
        return logp if log else np.exp(logp)


class BayesianMVLinReg(ConjPrior):
    r"""
    Bayesian multivariate linear regression conjugate prior, which models a multivariate input as a function of time.
    Following `Wikipedia <https://en.wikipedia.org/wiki/Bayesian_multivariate_linear_regression>`__ and
    `Geisser (1965) <https://www.jstor.org/stable/2238083>`__, we assume the model

    .. math::

        \begin{align*}
        X(t) &\sim \mathcal{N}_{d}(m t + b, \Sigma) \\
        (m, b) &\sim \mathcal{N}_{2d}((m_0, b_0), \Sigma \otimes \Lambda_0^{-1}) \\
        \Sigma &\sim \mathrm{InvWishart}_{\nu}(V_0) \\
        \end{align*}

    where :math:`(m, b)` is the concatenation of the vectors :math:`m` and :math:`b`,
    :math:`\Lambda_0 \in \mathbb{R}^{2 \times 2}`, and :math:`\otimes` is the Kronecker product.
    Consider new data :math:`(t_1, x_1), \ldots, (t_n, x_n)`. Let :math:`T \in \mathbb{R}^{n \times 2}` be
    the matrix obtained by stacking the row vector of times with an all-ones row vector. Let
    :math:`W = [m, b]^T \in \mathbb{R}^{2 \times d}` be the full weight matrix. Let
    :math:`X \in \mathbb{R}^{n \times d}` be the matrix of observed :math:`x` values. Then we have the update rule

    .. math::
        \begin{align*}
        \nu_n &= \nu_0 + n \\
        W_n &= (\Lambda_0 + T^T T)^{-1}(\Lambda_0 W_0 + T^T X) \\
        V_n &= V_0 + (X - TW_n)^T (X - TW_n) + (W_n - W_0)^T \Lambda_0 (W_n - W_0) \\
        \Lambda_n &= \Lambda_0 + T^T T \\
        \end{align*}

    """

    def __init__(self, sample=None):
        super().__init__()
        self.nu = 0
        self.w_0 = None
        self.Lambda_0 = np.zeros((2, 2))
        self.V_0 = None
        if sample is not None:
            self.update(sample)

    def update(self, x):
        t, x = self.process_time_series(x)
        n, d = x.shape
        if self.V_0 is None:
            self.V_0 = np.zeros((d, d))
        if self.w_0 is None:
            self.w_0 = np.zeros((2, d))

        t_full = np.stack((t, np.ones_like(t)), axis=-1)  # [n, 2]
        design = t_full.T @ t_full
        new_Lambda = design + self.Lambda_0
        new_w = pinvh(new_Lambda) @ (t_full.T @ x + self.Lambda_0 @ self.w_0)

        self.n = self.n + n
        self.nu = self.nu + n
        residual = x - t_full @ new_w  # [n, d]
        delta_w = new_w - self.w_0  # [2, d]
        self.V_0 = self.V_0 + residual.T @ residual + delta_w.T @ self.Lambda_0 @ delta_w
        self.w_0 = new_w
        self.Lambda_0 = new_Lambda

    def posterior(self, x, return_rv=False, log=True, return_updated=False):
        r"""
        Naive computation of the posterior using Bayes Rule, i.e.

        .. math::

            \hat{\Sigma} &= \mathbb{E}[\Sigma] \\
            \hat{W} &= \mathbb{E}[W \mid \Sigma = \hat{\Sigma}] \\
            p(X \mid t) &= \frac{
            p(W = \hat{W}, \Sigma = \hat{\Sigma})
            p(X \mid t, W = \hat{W}, \Sigma = \hat{\Sigma})}{
            p(W = \hat{W}, \Sigma = \hat{\Sigma} \mid x, t)}

        """
        if x is None or return_rv:
            raise ValueError(
                "Bayesian linear regression doesn't have a scipy.stats random variable posterior. "
                "Please specify a non-``None`` value of ``x`` and set ``return_rv = False``."
            )
        t, x_np = self.process_time_series(x)

        # Get priors & MAP estimates for Sigma and W; get the MAP estimate for x(t)
        prior_Sigma = invwishart(df=self.nu, scale=self.V_0)
        Sigma_hat = prior_Sigma.mean()
        w_hat = self.w_0.flatten()
        prior_w = mvnorm(w_hat, np.kron(Sigma_hat, pinvh(self.Lambda_0)))
        xhat = np.stack((t, np.ones_like(t)), axis=-1) @ w_hat.reshape(2, -1)

        # Get posteriors
        updated = copy.deepcopy(self)
        updated.update(x)
        post_Sigma = invwishart(df=updated.nu, scale=updated.V_0)
        post_w = mvnorm(updated.w_0.flatten(), np.kron(Sigma_hat, pinvh(updated.Lambda_0)))

        # Apply Bayes' rule
        evidence = mvnorm(xhat, Sigma_hat).logpdf(x_np).reshape(len(x_np))
        prior = prior_Sigma.logpdf(Sigma_hat) + prior_w.logpdf(w_hat)
        post = post_Sigma.logpdf(Sigma_hat) + post_w.logpdf(w_hat)
        logp = evidence + prior - post

        ret = logp if log else np.exp(logp)
        return (ret, updated) if return_updated else ret